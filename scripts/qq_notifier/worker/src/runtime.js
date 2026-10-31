import { createPrivateKey, createPublicKey, sign, timingSafeEqual, verify } from 'node:crypto';
import { formatEvent, splitAnnouncement } from './format.js';

const seconds = () => Math.floor(Date.now() / 1000);
const fatalGroupCodes = new Set([40034101, 40034105, 40054003, 40054002]);

function equalSecret(actual, expected) {
  const left = Buffer.from(actual);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

async function readBody(request) {
  const reader = request.body?.getReader();
  if (!reader) throw Object.assign(new Error('Missing body'), { status: 400 });
  const chunks = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.length;
    if (length > 4 * 1024 * 1024) {
      await reader.cancel();
      throw Object.assign(new Error('Payload too large'), { status: 413 });
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks, length);
}

function parseBody(body) {
  try {
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw Object.assign(new Error('Invalid JSON'), { status: 400 }); }
}

async function githubSignature(request, body, env) {
  const hex = request.headers.get('x-hub-signature-256') ?? '';
  if (!/^sha256=[0-9a-f]{64}$/i.test(hex)) return false;
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(env.GITHUB_WEBHOOK_SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
  return crypto.subtle.verify('HMAC', key, Buffer.from(hex.slice(7), 'hex'), body);
}

function qqPrivateKey(env) {
  const secret = Buffer.from(env.QQ_APP_SECRET);
  const seed = Buffer.alloc(32);
  for (let i = 0; i < seed.length; i++) seed[i] = secret[i % secret.length];
  return createPrivateKey({ key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed]), format: 'der', type: 'pkcs8' });
}

async function qqAccessToken(env) {
  const cached = await env.DB.prepare("SELECT value FROM runtime_state WHERE key='qq-token' AND expires_at>?").bind(seconds() + 60).first();
  if (cached) return cached.value;
  const response = await fetch('https://bots.qq.com/app/getAppAccessToken', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ appId: env.QQ_APP_ID, clientSecret: env.QQ_APP_SECRET }), signal: AbortSignal.timeout(8000),
  });
  const data = await response.json();
  if (!response.ok || typeof data.access_token !== 'string') throw Object.assign(new Error('QQ authorization failed'), { qqStatus: response.status, code: data.code });
  await env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('qq-token',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at")
    .bind(data.access_token, seconds() + Number(data.expires_in)).run();
  return data.access_token;
}

async function enqueue(env, key, kind, parts, groupId = null, replyMsgId = null, changes = []) {
  const now = seconds();
  const encoded = JSON.stringify(parts);
  await env.DB.batch([
    ...changes,
    env.DB.prepare('INSERT OR IGNORE INTO events(id,kind,parts,created) VALUES(?,?,?,?)').bind(key, kind, encoded, now),
    env.DB.prepare(`INSERT OR IGNORE INTO notifications(id,event_id,group_id,parts,created,due,reply_msg_id)
      SELECT e.id || ':' || s.group_id,e.id,s.group_id,e.parts,?,?,? FROM events e CROSS JOIN subscriptions s
      WHERE e.id=? AND e.enqueued=0 AND s.enabled=1 AND (? IS NULL OR s.group_id=?)`)
      .bind(now, now, replyMsgId, key, groupId, groupId),
    env.DB.prepare('UPDATE events SET enqueued=1 WHERE id=?').bind(key),
  ]);
}

async function dispatch(env) {
  if (env.DELIVERY_ENABLED !== 'true') return;
  const started = Date.now();
  const lease = crypto.randomUUID();
  const acquired = await env.DB.prepare(`INSERT INTO runtime_state(key,value,expires_at) VALUES('delivery-lock',?,?)
    ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at WHERE expires_at<?`)
    .bind(lease, seconds() + 90, seconds()).run();
  if (!acquired.meta.changes) return;
  try {
    for (let i = 0; i < 6 && Date.now() - started < 12000; i++) {
      const row = await env.DB.prepare(`SELECT n.* FROM notifications n JOIN subscriptions s ON s.group_id=n.group_id
        WHERE n.sent=0 AND n.attempts<12 AND n.due<=? AND s.enabled=1 ORDER BY n.created,n.id LIMIT 1`).bind(seconds()).first();
      if (!row) break;
      const rate = await env.DB.prepare("SELECT expires_at FROM runtime_state WHERE key='next-send'").first();
      const wait = Math.max(0, (rate?.expires_at ?? 0) * 1000 - Date.now());
      // Reserve 16 seconds for authorization plus delivery within waitUntil's 30-second window.
      if (Date.now() - started + wait > 12000) break;
      if (wait) await new Promise(resolve => setTimeout(resolve, wait));
      const parts = JSON.parse(row.parts);
      try {
        const token = await qqAccessToken(env);
        const response = await fetch(`https://api.sgroup.qq.com/v2/groups/${encodeURIComponent(row.group_id)}/messages`, {
          method: 'POST', headers: { Authorization: `QQBot ${token}`, 'X-Union-Appid': env.QQ_APP_ID, 'content-type': 'application/json' },
          body: JSON.stringify({ msg_type: 0, content: parts[row.next_part], timestamp: row.created,
            ...(row.reply_msg_id ? { msg_id: row.reply_msg_id, msg_seq: row.next_part + 1 } : {}) }), signal: AbortSignal.timeout(8000),
        });
        const data = await response.json();
        if (!response.ok || !data.id) throw Object.assign(new Error('QQ delivery failed'), { qqStatus: response.status, code: data.code });
        await env.DB.prepare('UPDATE notifications SET next_part=?,sent=?,attempts=0,due=? WHERE id=?')
          .bind(row.next_part + 1, Number(row.next_part + 1 >= parts.length), seconds(), row.id).run();
        console.log(JSON.stringify({ event: 'notification_delivered', part: row.next_part + 1, total: parts.length }));
      } catch (error) {
        console.warn(JSON.stringify({ event: 'qq_delivery_failed', type: error.name, status: error.qqStatus ?? null, code: error.code ?? null }));
        const changes = [env.DB.prepare('UPDATE notifications SET attempts=attempts+1,due=? WHERE id=?').bind(seconds() + Math.min(3600, 30 * 2 ** row.attempts), row.id)];
        if (fatalGroupCodes.has(error.code)) changes.push(env.DB.prepare('UPDATE subscriptions SET enabled=0 WHERE group_id=?').bind(row.group_id));
        if (error.qqStatus === 401) changes.push(env.DB.prepare("DELETE FROM runtime_state WHERE key='qq-token'"));
        await env.DB.batch(changes);
      }
      await env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('next-send','',?) ON CONFLICT(key) DO UPDATE SET expires_at=excluded.expires_at").bind(seconds() + 4).run();
    }
  } finally {
    await env.DB.prepare("DELETE FROM runtime_state WHERE key='delivery-lock' AND value=?").bind(lease).run();
  }
}

async function githubWebhook(request, env, ctx) {
  const body = await readBody(request);
  if (!await githubSignature(request, body, env)) return new Response('Invalid signature', { status: 401 });
  const payload = parseBody(body);
  const type = request.headers.get('x-github-event');
  if (type === 'ping') return Response.json({ ok: true });
  if (String(payload.repository?.full_name ?? '').toLowerCase() !== env.REPOSITORY.toLowerCase()) return new Response('Repository not allowed', { status: 403 });
  const delivery = request.headers.get('x-github-delivery');
  if (!delivery || delivery.length > 100) return new Response('Invalid delivery ID', { status: 400 });
  const message = formatEvent(type, payload, env);
  if (!message) return Response.json({ ok: true });
  let key = `delivery:${delivery}`;
  if (type === 'release') {
    if (!Number.isSafeInteger(payload.release?.id) || payload.release.id <= 0) return new Response('Invalid release ID', { status: 400 });
    key = `release:${payload.release.id}`;
  }
  await enqueue(env, key, type, type === 'release' ? splitAnnouncement(message) : [message]);
  ctx.waitUntil(dispatch(env));
  return Response.json({ ok: true });
}

async function sendReply(env, group, messageId, content) {
  const token = await qqAccessToken(env);
  const response = await fetch(`https://api.sgroup.qq.com/v2/groups/${encodeURIComponent(group)}/messages`, {
    method: 'POST', headers: { Authorization: `QQBot ${token}`, 'X-Union-Appid': env.QQ_APP_ID, 'content-type': 'application/json' },
    body: JSON.stringify({ msg_type: 0, content, msg_id: messageId, msg_seq: 1 }), signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) console.warn(JSON.stringify({ event: 'qq_reply_failed', status: response.status }));
}

async function qqWebhook(request, env, ctx) {
  const body = await readBody(request);
  const payload = parseBody(body);
  const privateKey = qqPrivateKey(env);
  if (payload.op === 13) {
    const { plain_token: plainToken, event_ts: eventTs } = payload.d ?? {};
    if (typeof plainToken !== 'string' || typeof eventTs !== 'string') return new Response('Invalid challenge', { status: 400 });
    return Response.json({ plain_token: plainToken, signature: sign(null, Buffer.from(eventTs + plainToken), privateKey).toString('hex') });
  }
  const signature = request.headers.get('x-signature-ed25519') ?? '';
  const timestamp = request.headers.get('x-signature-timestamp') ?? '';
  if (!/^[0-9a-f]{128}$/i.test(signature) || !timestamp || !verify(null, Buffer.concat([Buffer.from(timestamp), body]), createPublicKey(privateKey), Buffer.from(signature, 'hex'))) return new Response('Invalid signature', { status: 401 });
  const event = payload.d ?? {};
  const group = event.group_openid;
  if (typeof group !== 'string' || !group) return Response.json({ op: 12 });
  if (payload.t === 'GROUP_DEL_ROBOT') {
    await env.DB.batch([env.DB.prepare('DELETE FROM notifications WHERE group_id=?').bind(group), env.DB.prepare('DELETE FROM subscriptions WHERE group_id=?').bind(group)]);
  } else if (['GROUP_MSG_RECEIVE', 'GROUP_MSG_REJECT'].includes(payload.t)) {
    await env.DB.prepare('UPDATE subscriptions SET enabled=? WHERE group_id=?').bind(Number(payload.t === 'GROUP_MSG_RECEIVE'), group).run();
  } else if (payload.t === 'GROUP_AT_MESSAGE_CREATE') {
    const text = String(event.content ?? '').replace(/<@!?\d+>/g, '').trim().replace(/^\//, '');
    const [command, ...args] = text.split(/\s+/);
    if (!['绑定GitHub', '取消GitHub', 'GitHub状态', 'GitHub帮助'].includes(command) || !event.id) return Response.json({ op: 12 });
    let response;
    const changes = [];
    if (command === '绑定GitHub' || command === '取消GitHub') {
      if (!equalSecret(args.join(' '), env.BINDING_CODE)) response = '绑定码不正确，请联系机器人管理员。';
      else if (command === '绑定GitHub') {
        const other = await env.DB.prepare('SELECT 1 AS found FROM subscriptions WHERE group_id<>? LIMIT 1').bind(group).first();
        if (other) response = '机器人已绑定其他群，请联系机器人管理员更换。';
        else {
          changes.push(env.DB.prepare(`INSERT INTO subscriptions(group_id,enabled) SELECT ?,1 WHERE NOT EXISTS(SELECT 1 FROM subscriptions WHERE group_id<>?)
            ON CONFLICT(group_id) DO UPDATE SET enabled=1`).bind(group, group));
          response = `已订阅 ${env.REPOSITORY} 的提交、Issue、PR、版本发布和工作流异常。请在机器人资料中开启主动消息。`;
        }
      } else {
        await env.DB.batch([env.DB.prepare('DELETE FROM notifications WHERE group_id=?').bind(group), env.DB.prepare('DELETE FROM subscriptions WHERE group_id=?').bind(group)]);
        ctx.waitUntil(sendReply(env, group, event.id, '本群已取消 GitHub 通知。'));
        return Response.json({ op: 12 });
      }
    } else if (command === 'GitHub状态') {
      const subscription = await env.DB.prepare('SELECT enabled FROM subscriptions WHERE group_id=?').bind(group).first();
      const counts = await env.DB.prepare('SELECT SUM(sent=0 AND attempts<12) AS pending,SUM(sent=0 AND attempts>=12) AS failed FROM notifications WHERE group_id=?').bind(group).first();
      response = `${env.REPOSITORY}\n通知状态：${!subscription ? '未绑定' : subscription.enabled ? '接收中' : '已暂停，请开启主动消息后重新绑定'}\n待发送：${counts.pending ?? 0} 条；发送失败：${counts.failed ?? 0} 条`;
    } else response = 'GitHub 通知指令：\n绑定GitHub 绑定码\nGitHub状态\n取消GitHub 绑定码\n请先 @机器人，再发送指令。';
    if (changes.length) {
      await enqueue(env, `qq:${event.id}`, 'reply', [response], group, event.id, changes);
      ctx.waitUntil(dispatch(env));
    } else ctx.waitUntil(sendReply(env, group, event.id, response));
  }
  return Response.json({ op: 12 });
}

async function status(env) {
  const groups = await env.DB.prepare('SELECT count(*) AS count FROM subscriptions WHERE enabled=1').first();
  const counts = await env.DB.prepare('SELECT SUM(sent=0 AND attempts<12) AS pending,SUM(sent=0 AND attempts>=12) AS failed,SUM(sent=1) AS sent FROM notifications').first();
  let qq;
  try {
    const token = await qqAccessToken(env);
    const response = await fetch('https://api.sgroup.qq.com/users/@me', { headers: { Authorization: `QQBot ${token}`, 'X-Union-Appid': env.QQ_APP_ID }, signal: AbortSignal.timeout(8000) });
    const data = await response.json();
    qq = { ok: response.ok && !!data.id, status: response.status, code: data.code ?? null };
  } catch (error) { qq = { ok: false, status: error.qqStatus ?? null, code: error.code ?? null }; }
  return Response.json({ delivery_enabled: env.DELIVERY_ENABLED === 'true', groups: groups.count, pending: counts.pending ?? 0, failed: counts.failed ?? 0, sent: counts.sent ?? 0, qq }, { headers: { 'cache-control': 'no-store' } });
}

export default {
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);
      if (request.method === 'GET' && url.pathname === '/health') return Response.json({ ok: true, service: 'maanikki-qq-notifier' });
      if (request.method === 'GET' && url.pathname === '/admin/status') {
        return env.ADMIN_TOKEN && equalSecret(request.headers.get('authorization') || '', `Bearer ${env.ADMIN_TOKEN}`)
          ? await status(env) : new Response('Unauthorized', { status: 401 });
      }
      if (request.method === 'POST' && url.pathname === '/github/webhook') return await githubWebhook(request, env, ctx);
      if (request.method === 'POST' && url.pathname === '/qq/webhook') return await qqWebhook(request, env, ctx);
      return new Response('Not Found', { status: 404 });
    } catch (error) {
      console.error(JSON.stringify({ event: 'request_failed', type: error.name, status: error.status ?? 500 }));
      return new Response(error.status ? error.message : 'Internal error', { status: error.status ?? 500 });
    }
  },
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatch(env));
    if (Math.floor(controller.scheduledTime / 60000) % 60 === 0) ctx.waitUntil(env.DB.batch([
      env.DB.prepare('DELETE FROM notifications WHERE sent=1 AND created<?').bind(seconds() - 30 * 86400),
      env.DB.prepare("DELETE FROM events WHERE kind NOT IN ('release','legacy_release') AND created<? AND id NOT IN (SELECT event_id FROM notifications)").bind(seconds() - 7 * 86400),
    ]));
  },
};
