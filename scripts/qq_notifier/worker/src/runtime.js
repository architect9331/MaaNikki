import { createPrivateKey, createPublicKey, sign, timingSafeEqual, verify } from 'node:crypto';
import {
  formatActivitiesReply,
  formatRedeemCodesReply,
  getFeedsContextSummary,
  syncActivities,
  syncRedeemCodes,
} from './feeds.js';
import { formatEvent, sanitizeUrls, splitAnnouncement } from './format.js';
import { HELP_TEXT, KNOWLEDGE_PROMPT, WELCOME_TEXT } from './knowledge.js';

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
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ appId: env.QQ_APP_ID, clientSecret: env.QQ_APP_SECRET }),
    signal: AbortSignal.timeout(8000),
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
      if (Date.now() - started + wait > 12000) break;
      if (wait) await new Promise(resolve => setTimeout(resolve, wait));
      const parts = JSON.parse(row.parts);
      try {
        const token = await qqAccessToken(env);
        const response = await fetch(`https://api.sgroup.qq.com/v2/groups/${encodeURIComponent(row.group_id)}/messages`, {
          method: 'POST',
          headers: { Authorization: `QQBot ${token}`, 'X-Union-Appid': env.QQ_APP_ID, 'content-type': 'application/json' },
          body: JSON.stringify({
            msg_type: 0,
            content: parts[row.next_part],
            timestamp: row.created,
            ...(row.reply_msg_id ? { msg_id: row.reply_msg_id, msg_seq: row.next_part + 1 } : {}),
          }),
          signal: AbortSignal.timeout(8000),
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

async function sendReply(env, group, messageId, content, msgSeq = 1) {
  const token = await qqAccessToken(env);
  const post = async (text, seq) => fetch(`https://api.sgroup.qq.com/v2/groups/${encodeURIComponent(group)}/messages`, {
    method: 'POST',
    headers: { Authorization: `QQBot ${token}`, 'X-Union-Appid': env.QQ_APP_ID, 'content-type': 'application/json' },
    body: JSON.stringify({ msg_type: 0, content: text, ...(messageId ? { msg_id: messageId, msg_seq: seq } : {}) }),
    signal: AbortSignal.timeout(8000),
  });
  let response = await post(content, msgSeq);
  if (!response.ok) {
    const safeText = sanitizeUrls(content);
    if (safeText !== content) response = await post(safeText, msgSeq + 1);
  }
  if (!response.ok) {
    const errText = await response.text().catch(() => '');
    console.warn(JSON.stringify({ event: 'qq_reply_failed', status: response.status, detail: errText.slice(0, 200) }));
    return false;
  }
  return true;
}

async function getRecentProjectUpdates(env) {
  try {
    const rels = await env.DB.prepare("SELECT parts FROM events WHERE kind='release' ORDER BY created DESC LIMIT 2").all();
    const evs = await env.DB.prepare("SELECT parts FROM events WHERE kind IN ('push','issues','pull_request') ORDER BY created DESC LIMIT 3").all();
    const sections = [];
    const relTexts = (rels?.results || []).map(r => {
      try {
        const p = JSON.parse(r.parts);
        return Array.isArray(p) ? p.join('\n') : '';
      } catch { return ''; }
    }).filter(Boolean);
    if (relTexts.length) sections.push(`【最新已发布版本更新公告】\n${relTexts.join('\n\n').slice(0, 1400)}`);
    const recent = (evs?.results || []).map(r => {
      try {
        const arr = JSON.parse(r.parts);
        return Array.isArray(arr) ? arr[0] : '';
      } catch { return ''; }
    }).filter(Boolean);
    if (recent.length) sections.push(`【近期仓库提交与动态】\n${recent.join('\n---\n').slice(0, 500)}`);
    const feedsSummary = await getFeedsContextSummary(env);
    if (feedsSummary) sections.push(feedsSummary);
    return sections.join('\n\n');
  } catch {
    return '';
  }
}

function extractMessageInfo(event, isExplicitAt) {
  const raw = String(event.content ?? '').replace(/<faceType=[^>]+>/g, '[表情]');
  const hasBotAtTag = /<@!?(?:\d+|7165D8ED90FF2AE58C3E181A57B5CF42)>/i.test(raw);
  const hasOtherAtTag = !hasBotAtTag && !isExplicitAt && /<@!?[0-9A-Za-z_]+>/.test(raw);
  let cleanText = raw.replace(/<@!?[0-9A-Za-z_]+>/g, '').replace(/^[“"'「『\s]+|[”"'」』\s]+$/g, '').trim();
  const prefixMatch = cleanText.match(/^@(?:Arc9332|MaaNikki|机器人|小助手)\s*/i)
    || cleanText.match(/^(?:Arc9332|MaaNikki|机器人|小助手)(?:[，,：:\s]+|$)/i);
  if (prefixMatch) cleanText = cleanText.slice(prefixMatch[0].length).replace(/^[“"'「『\s]+|[”"'」』\s]+$/g, '').trim();
  const mentioned = Boolean(isExplicitAt || hasBotAtTag || prefixMatch);
  const senderId = String(event.author?.member_openid ?? event.author?.id ?? 'unknown');
  const rawName = String(event.author?.username ?? event.author?.nick ?? '').trim();
  const senderName = rawName || `群友${senderId.slice(-4)}`;
  const ext = Array.isArray(event.message_scene?.ext) ? event.message_scene.ext : [];
  let msgIdx = '';
  let refMsgIdx = '';
  for (const item of ext) {
    if (typeof item === 'string' && item.startsWith('msg_idx=')) msgIdx = item.slice(8);
    else if (typeof item === 'string' && item.startsWith('ref_msg_idx=')) refMsgIdx = item.slice(12);
  }
  const imageUrls = [];
  if (Array.isArray(event.attachments)) {
    for (const att of event.attachments) {
      let u = String(att?.url ?? '').trim();
      if (!u) continue;
      const ct = String(att?.content_type ?? '').toLowerCase();
      if (ct.startsWith('image/') || /\.(?:png|jpe?g|webp|gif)(?:\?|$)/i.test(u) || u.includes('multimedia.nt.qq.com.cn')) {
        if (u.startsWith('//')) u = `https:${u}`;
        else if (!u.startsWith('http')) u = `https://${u.replace(/^\/+/, '')}`;
        imageUrls.push(u);
      }
    }
  }
  return { cleanText, mentioned, hasOtherAtTag, senderId, senderName, msgIdx, refMsgIdx, imageUrls };
}

async function recordGroupMessage(env, group, msgId, info, isBot = 0) {
  const now = seconds();
  let quoteText = '';
  if (info.refMsgIdx) {
    const quoted = await env.DB.prepare('SELECT sender_name, content FROM group_messages WHERE group_id=? AND msg_idx=? ORDER BY id DESC LIMIT 1')
      .bind(group, info.refMsgIdx).first().catch(() => null);
    if (quoted) quoteText = `${quoted.sender_name || '群友'}: ${String(quoted.content || '').slice(0, 120)}`;
  }
  await env.DB.prepare(`INSERT INTO group_messages(group_id,msg_id,msg_idx,sender_id,sender_name,role,is_bot,mentioned,content,quote_text,image_urls,created)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
    group,
    String(msgId || crypto.randomUUID()),
    info.msgIdx || null,
    info.senderId,
    info.senderName,
    isBot ? 'assistant' : 'user',
    isBot ? 1 : 0,
    info.mentioned ? 1 : 0,
    info.cleanText || (info.imageUrls?.length ? '[图片]' : ''),
    quoteText || null,
    info.imageUrls?.length ? JSON.stringify(info.imageUrls) : null,
    now,
  ).run().catch(() => {});
  return quoteText;
}

async function getAssistantState(env, group) {
  const row = await env.DB.prepare('SELECT * FROM group_assistant_state WHERE group_id=?').bind(group).first().catch(() => null);
  return row || {
    group_id: group,
    muted_until: 0,
    last_welcome_at: 0,
    last_auto_reply_at: 0,
    last_reply_at: 0,
    last_reply_sender: '',
    auto_reply_times: '[]',
  };
}

function canConsiderAutonomous(text, imageUrls, state, senderId, hasOtherAtTag = false) {
  if (hasOtherAtTag) return false;
  const t = String(text ?? '').trim();
  if (!t && (!imageUrls || !imageUrls.length)) return false;
  if (t.startsWith('/')) return false;
  if (/^(?:哈哈+|666+|牛逼|收到|好的|好滴|谢谢|多谢|嗯+|哦+|草+|卧槽|\?+|？+|!+|！+|\.+|。+|\[表情\]|\s)+$/i.test(t)) return false;
  const now = seconds();
  if (state.muted_until > now) return false;
  if (state.last_reply_sender === senderId && now - (state.last_reply_at || 0) <= 90) return true;
  const lower = t.toLowerCase();
  if (/maanikki|maa|机器人|小助手|arc9332/.test(lower)) return true;
  const hasDomain = /版本|最新版|新版|更新|修复|改动|功能|分辨率|16:9|1920|管理员|后台|前台|遮挡|最小化|下载|官网|连接|启动|美鸭梨|挖掘|朝夕心愿|心愿|每日|星海|拾光|星光结晶|星之铃|家园|钓星|巨陨星|幻境|追光|体力|祝福闪光|胚子|全收集|制衣|焕新|采集|钓鱼|跑图|路线|噗灵|衣柜|读取|奇迹之冠|巅峰赛|搭配|标点|标记|地图|奇想星|宝箱|露珠|共鸣|暖暖|大喵|日志|活动|联动|前瞻|直播|兑换码|礼包码|福利码|维护|停服|什么时候结束|几点结束/.test(lower);
  const hasQuestionOrTrouble = /[?？]|怎么|如何|为什么|为啥|什么|啥|哪|吗|呢|吧|么|请问|求助|有没有|能不能|支持|可以|报错|失败|卡住|不动|闪退|没反应|识别不到|点不动|黑屏|偏航|卡墙/.test(lower);
  return (hasDomain && hasQuestionOrTrouble) || (hasQuestionOrTrouble && t.length >= 6);
}

async function fetchImageAsDataUrl(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) return null;
    const buf = Buffer.from(await res.arrayBuffer());
    if (!buf.length || buf.length > 3500000) return null;
    const mime = (res.headers.get('content-type') || 'image/jpeg').split(';')[0].trim();
    return `data:${mime.startsWith('image/') ? mime : 'image/jpeg'};base64,${buf.toString('base64')}`;
  } catch {
    return null;
  }
}

function cleanAssistantReply(raw) {
  let text = String(raw ?? '').trim();
  if (!text || text.includes('[[QQ_SILENT]]')) return '';
  text = text.replace(/```[\s\S]*?```/g, m => m.replace(/```\w*\n?/g, '')).replace(/^#{1,6}\s+/gm, '').replace(/\*\*|__/g, '').replace(/`([^`]+)`/g, '$1').trim();
  text = sanitizeUrls(text);
  if (text.length > 450) text = `${text.slice(0, 445).trimEnd()}…`;
  return text;
}

async function callAssistantLLM(env, group, info, quoteText, mode) {
  const apiKey = String(env.LLM_API_KEY || '').trim();
  const endpoints = String(env.LLM_API_BASE || '').split(',')
    .map(s => s.trim())
    .filter(Boolean)
    .map(base => `${base.replace(/\/+$/, '')}/chat/completions`);
  if (!apiKey || !endpoints.length) {
    return mode === 'explicit' ? '助手问答服务暂未配置，可先发送「@我 帮助」查看常用指令。' : '';
  }

  const rows = await env.DB.prepare('SELECT sender_name,is_bot,content,quote_text,image_urls,created FROM group_messages WHERE group_id=? ORDER BY id DESC LIMIT 14')
    .bind(group).all().catch(() => ({ results: [] }));
  const history = (rows.results || []).slice().reverse();
  const contextLines = history.map(r => {
    const who = r.is_bot ? '[机器人]' : `[${r.sender_name || '群友'}]`;
    const q = r.quote_text ? ` (引用 ${r.quote_text})` : '';
    const img = r.image_urls ? ' [含图片]' : '';
    return `${who}${q}: ${r.content || ''}${img}`;
  });
  const candidateUrls = [...(info.imageUrls || [])];
  if (!candidateUrls.length) {
    for (let i = history.length - 1; i >= 0 && candidateUrls.length < 2; i--) {
      if (history[i].image_urls) {
        try {
          for (const u of JSON.parse(history[i].image_urls)) if (candidateUrls.length < 2) candidateUrls.push(u);
        } catch {}
      }
    }
  }
  const dataUrls = [];
  for (const u of candidateUrls.slice(0, 2)) {
    const d = await fetchImageAsDataUrl(u);
    if (d) dataUrls.push(d);
  }
  const modeRule = mode === 'explicit'
    ? '当前模式：用户主动 @ 了你（或对你的回复追问）。请结合上方最近群聊上下文、引用内容及附图，直接给出准确、干练的解答。如果用户说「看看上面」「帮他解答」，请针对上下文里群友遇到的问题作答。'
    : '当前模式：群聊旁听自动触发（用户未 @ 你）。请严格判断：只有当该消息确实是在询问 MaaNikki 或无限暖暖活动/兑换码/自动化相关问题、求助报错、且上下文里还没有人给出准确解答时，才简短解答；如果是群友之间闲聊、开玩笑、与本工具及暖暖活动无关、已有人解答、或你拿不准，请只输出 [[QQ_SILENT]] 不要输出任何其他字符！';
  const userPrompt = [
    '【最近群聊上下文】',
    contextLines.join('\n') || '（无历史消息）',
    '',
    `【当前触发消息】发送者：${info.senderName}`,
    quoteText ? `引用内容：${quoteText}` : '',
    `消息内容：${info.cleanText || '（用户仅 @ 了你或发送了图片，请结合上下文与图片解答）'}`,
  ].filter(Boolean).join('\n');
  const contentParts = dataUrls.length
    ? [{ type: 'text', text: userPrompt }, ...dataUrls.map(url => ({ type: 'image_url', image_url: { url } }))]
    : userPrompt;
  const recentUpdates = await getRecentProjectUpdates(env);
  const systemPrompt = [KNOWLEDGE_PROMPT, recentUpdates, modeRule].filter(Boolean).join('\n\n');
  const model = env.LLM_MODEL || 'gpt-6.1-sol';

  let lastErr = null;
  for (const url of endpoints) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { Authorization: `Bearer ${apiKey}`, 'content-type': 'application/json' },
        body: JSON.stringify({
          model,
          temperature: 0.3,
          max_tokens: 450,
          messages: [
            { role: 'system', content: systemPrompt },
            { role: 'user', content: contentParts },
          ],
        }),
        signal: AbortSignal.timeout(18000),
      });
      if (!res.ok) {
        lastErr = `${res.status}`;
        continue;
      }
      const data = await res.json();
      const reply = data?.choices?.[0]?.message?.content;
      if (typeof reply === 'string') return cleanAssistantReply(reply);
    } catch (e) {
      lastErr = e.message;
    }
  }
  console.warn(JSON.stringify({ event: 'llm_call_failed', error: lastErr }));
  return mode === 'explicit' ? '抱歉，模型服务暂时响应超时，请稍后再试或先输入「@我 帮助」查看常见问题。' : '';
}

async function sendAndRecordBotReply(env, group, messageId, text, senderId, isAuto = false) {
  const ok = await sendReply(env, group, messageId, text);
  if (!ok) return;
  const now = seconds();
  await recordGroupMessage(env, group, `bot:${messageId}:${now}`, {
    cleanText: text,
    mentioned: false,
    senderId: 'bot',
    senderName: 'Arc9332',
    msgIdx: '',
    refMsgIdx: '',
    imageUrls: [],
  }, 1);
  const state = await getAssistantState(env, group);
  let times = [];
  try {
    times = JSON.parse(state.auto_reply_times || '[]').filter(t => now - t < 300);
  } catch {}
  if (isAuto) times.push(now);
  await env.DB.prepare(`INSERT INTO group_assistant_state(group_id,muted_until,last_welcome_at,last_auto_reply_at,last_reply_at,last_reply_sender,auto_reply_times)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET
    last_auto_reply_at=excluded.last_auto_reply_at,last_reply_at=excluded.last_reply_at,last_reply_sender=excluded.last_reply_sender,auto_reply_times=excluded.auto_reply_times`).bind(
    group,
    state.muted_until || 0,
    state.last_welcome_at || 0,
    isAuto ? now : (state.last_auto_reply_at || 0),
    now,
    senderId || '',
    JSON.stringify(times),
  ).run().catch(() => {});
}

async function handleAssistantTurn(env, group, msgId, info, quoteText, mode) {
  const reply = await callAssistantLLM(env, group, info, quoteText, mode);
  if (!reply) return;
  await sendAndRecordBotReply(env, group, msgId, reply, info.senderId, mode === 'autonomous');
}

async function handleAutonomousMessage(env, group, msgId, info, quoteText) {
  await new Promise(r => setTimeout(r, 1800));
  const now = seconds();
  const state = await getAssistantState(env, group);
  if ((state.muted_until || 0) > now) return;
  const isFollowUp = state.last_reply_sender === info.senderId && now - (state.last_reply_at || 0) <= 90;
  if (!isFollowUp && now - (state.last_auto_reply_at || 0) < 20) return;
  let times = [];
  try {
    times = JSON.parse(state.auto_reply_times || '[]').filter(t => now - t < 300);
  } catch {}
  if (times.length >= 4) return;
  const newer = await env.DB.prepare('SELECT sender_id,mentioned,is_bot FROM group_messages WHERE group_id=? AND id>(SELECT id FROM group_messages WHERE group_id=? AND msg_id=? ORDER BY id DESC LIMIT 1)')
    .bind(group, group, String(msgId)).all().catch(() => ({ results: [] }));
  for (const r of newer.results || []) {
    if (r.is_bot || r.mentioned || r.sender_id === info.senderId) return;
  }
  await handleAssistantTurn(env, group, msgId, info, quoteText, isFollowUp ? 'explicit' : 'autonomous');
}

async function setGroupMute(env, group, minutes) {
  const mutedUntil = minutes > 0 ? seconds() + minutes * 60 : 0;
  await env.DB.prepare(`INSERT INTO group_assistant_state(group_id,muted_until) VALUES(?,?)
    ON CONFLICT(group_id) DO UPDATE SET muted_until=excluded.muted_until`).bind(group, mutedUntil).run().catch(() => {});
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
  if (!/^[0-9a-f]{128}$/i.test(signature) || !timestamp || !verify(null, Buffer.concat([Buffer.from(timestamp), body]), createPublicKey(privateKey), Buffer.from(signature, 'hex'))) {
    return new Response('Invalid signature', { status: 401 });
  }
  const event = payload.d ?? {};
  const group = event.group_openid;
  if (typeof group !== 'string' || !group) return Response.json({ op: 12 });
  if (payload.t === 'GROUP_DEL_ROBOT') {
    await env.DB.batch([
      env.DB.prepare('DELETE FROM notifications WHERE group_id=?').bind(group),
      env.DB.prepare('DELETE FROM subscriptions WHERE group_id=?').bind(group),
    ]);
  } else if (['GROUP_MSG_RECEIVE', 'GROUP_MSG_REJECT'].includes(payload.t)) {
    await env.DB.prepare('UPDATE subscriptions SET enabled=? WHERE group_id=?').bind(Number(payload.t === 'GROUP_MSG_RECEIVE'), group).run();
  } else if (['GROUP_ADD_ROBOT', 'GROUP_MEMBER_ADD'].includes(payload.t)) {
    const state = await getAssistantState(env, group);
    const now = seconds();
    if (now - (state.last_welcome_at || 0) >= 300) {
      await env.DB.prepare('INSERT INTO group_assistant_state(group_id,last_welcome_at) VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET last_welcome_at=excluded.last_welcome_at')
        .bind(group, now).run().catch(() => {});
      ctx.waitUntil(sendReply(env, group, event.id || null, WELCOME_TEXT));
    }
  } else if (payload.t === 'GROUP_AT_MESSAGE_CREATE' || payload.t === 'GROUP_MESSAGE_CREATE') {
    if (!event.id) return Response.json({ op: 12 });
    const dup = await env.DB.prepare('SELECT 1 AS found FROM group_messages WHERE group_id=? AND msg_id=? LIMIT 1').bind(group, String(event.id)).first().catch(() => null);
    if (dup) return Response.json({ op: 12 });
    const isExplicitAt = payload.t === 'GROUP_AT_MESSAGE_CREATE';
    const info = extractMessageInfo(event, isExplicitAt);
    const quoteText = await recordGroupMessage(env, group, event.id, info, 0);
    if (info.mentioned) {
      const [rawCmd, ...args] = info.cleanText.replace(/^\//, '').split(/\s+/);
      const command = rawCmd || '';
      if (['绑定GitHub', '取消GitHub', 'GitHub状态', 'GitHub帮助'].includes(command)) {
        let response;
        const changes = [];
        if (command === '绑定GitHub' || command === '取消GitHub') {
          if (!equalSecret(args.join(' '), env.BINDING_CODE)) response = '绑定码不正确，请联系机器人管理员。';
          else if (command === '绑定GitHub') {
            if (await env.DB.prepare('SELECT 1 AS found FROM subscriptions WHERE group_id<>? LIMIT 1').bind(group).first()) {
              response = '机器人已绑定其他群，请联系机器人管理员更换。';
            } else {
              changes.push(env.DB.prepare(`INSERT INTO subscriptions(group_id,enabled) SELECT ?,1 WHERE NOT EXISTS(SELECT 1 FROM subscriptions WHERE group_id<>?)
                ON CONFLICT(group_id) DO UPDATE SET enabled=1`).bind(group, group));
              response = `已订阅 ${env.REPOSITORY} 的提交、Issue、PR、版本发布、工作流异常以及无限暖暖活动/兑换码推送。请在机器人资料中开启主动消息。`;
            }
          } else {
            await env.DB.batch([
              env.DB.prepare('DELETE FROM notifications WHERE group_id=?').bind(group),
              env.DB.prepare('DELETE FROM subscriptions WHERE group_id=?').bind(group),
            ]);
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
        return Response.json({ op: 12 });
      }
      const lowerCmd = command.toLowerCase();
      if (['帮助', 'help', '菜单', '功能', '指令'].includes(lowerCmd) && args.length === 0) {
        ctx.waitUntil(sendAndRecordBotReply(env, group, event.id, HELP_TEXT, info.senderId, false));
      } else if (['迎新', '新手', '新人', '上手', '教程'].includes(lowerCmd) && args.length === 0) {
        ctx.waitUntil(sendAndRecordBotReply(env, group, event.id, WELCOME_TEXT, info.senderId, false));
      } else if (['活动', '当前活动', '活动日历', '日历', '版本活动'].includes(lowerCmd) && args.length === 0) {
        ctx.waitUntil(formatActivitiesReply(env).then(reply => sendAndRecordBotReply(env, group, event.id, reply, info.senderId, false)));
      } else if (['兑换码', '礼包码', '福利码', '最新兑换码'].includes(lowerCmd) && args.length === 0) {
        ctx.waitUntil(formatRedeemCodesReply(env).then(reply => sendAndRecordBotReply(env, group, event.id, reply, info.senderId, false)));
      } else if (['安静', '闭嘴', '潜水', '静音'].includes(lowerCmd)) {
        const mins = Math.min(1440, Math.max(1, Number.parseInt(args[0] || '60', 10) || 60));
        await setGroupMute(env, group, mins);
        ctx.waitUntil(sendAndRecordBotReply(env, group, event.id, `好的，未来 ${mins} 分钟内我只响应 @ 消息，暂停自动接话（发送「@我 说话」可随时恢复）。`, info.senderId, false));
      } else if (['说话', '活跃', '解除静音', '恢复'].includes(lowerCmd) && args.length === 0) {
        await setGroupMute(env, group, 0);
        ctx.waitUntil(sendAndRecordBotReply(env, group, event.id, '已恢复自动接话模式。', info.senderId, false));
      } else if (['重置', '清空', '清空上下文', 'reset', 'clear'].includes(lowerCmd) && args.length === 0) {
        await env.DB.prepare('DELETE FROM group_messages WHERE group_id=?').bind(group).run().catch(() => {});
        ctx.waitUntil(sendReply(env, group, event.id, '已清空本群的近期对话上下文记忆。'));
      } else {
        ctx.waitUntil(handleAssistantTurn(env, group, event.id, info, quoteText, 'explicit'));
      }
    } else {
      const state = await getAssistantState(env, group);
      if (canConsiderAutonomous(info.cleanText, info.imageUrls, state, info.senderId, info.hasOtherAtTag)) {
        ctx.waitUntil(handleAutonomousMessage(env, group, event.id, info, quoteText));
      }
    }
  }
  return Response.json({ op: 12 });
}

async function pollExternalFeeds(env) {
  const now = seconds();
  const nextPoll = await env.DB.prepare("SELECT expires_at FROM runtime_state WHERE key='next-feed-poll'").first().catch(() => null);
  if (nextPoll && nextPoll.expires_at > now) return;

  const lease = crypto.randomUUID();
  const acquired = await env.DB.prepare(`INSERT INTO runtime_state(key,value,expires_at) VALUES('feed-poll-lock',?,?)
    ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at WHERE expires_at<?`)
    .bind(lease, now + 60, now).run();
  if (!acquired.meta.changes) return;

  let actStatus;
  let redeemStatus;
  try {
    try {
      actStatus = await syncActivities(env, enqueue);
    } catch (error) {
      actStatus = { error: String(error?.message || error).slice(0, 120) };
      console.warn(JSON.stringify({ event: 'activity_sync_failed', error: actStatus.error }));
    }
    try {
      redeemStatus = await syncRedeemCodes(env, enqueue);
    } catch (error) {
      redeemStatus = { error: String(error?.message || error).slice(0, 120) };
      console.warn(JSON.stringify({ event: 'redeem_sync_failed', error: redeemStatus.error }));
    }

    const deferSec = Number(redeemStatus?.deferSeconds || 0);
    const interval = deferSec > 0 ? deferSec : 600;
    const summary = JSON.stringify({ at: seconds(), activity: actStatus, redeem: redeemStatus });
    await env.DB.batch([
      env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('next-feed-poll','',?) ON CONFLICT(key) DO UPDATE SET expires_at=excluded.expires_at")
        .bind(seconds() + interval),
      env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('last-feed-sync',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at")
        .bind(summary, seconds() + 7 * 86400),
    ]);
  } finally {
    await env.DB.prepare("DELETE FROM runtime_state WHERE key='feed-poll-lock' AND value=?").bind(lease).run();
  }
}

async function status(env) {
  const groups = await env.DB.prepare('SELECT count(*) AS count FROM subscriptions WHERE enabled=1').first();
  const counts = await env.DB.prepare('SELECT SUM(sent=0 AND attempts<12) AS pending,SUM(sent=0 AND attempts>=12) AS failed,SUM(sent=1) AS sent FROM notifications').first();
  const msgCount = await env.DB.prepare('SELECT count(*) AS count FROM group_messages').first().catch(() => ({ count: 0 }));
  const codeCount = await env.DB.prepare('SELECT count(*) AS count FROM redeem_codes').first().catch(() => ({ count: 0 }));
  const actCache = await env.DB.prepare("SELECT value FROM runtime_state WHERE key='sra-activity-cache'").first().catch(() => null);
  const lastSync = await env.DB.prepare("SELECT value FROM runtime_state WHERE key='last-feed-sync'").first().catch(() => null);

  let qq;
  try {
    const token = await qqAccessToken(env);
    const response = await fetch('https://api.sgroup.qq.com/users/@me', { headers: { Authorization: `QQBot ${token}`, 'X-Union-Appid': env.QQ_APP_ID }, signal: AbortSignal.timeout(8000) });
    const data = await response.json();
    qq = { ok: response.ok && !!data.id, status: response.status, code: data.code ?? null };
  } catch (error) { qq = { ok: false, status: error.qqStatus ?? null, code: error.code ?? null }; }

  let parsedCache = null;
  let parsedSync = null;
  try { if (actCache?.value) parsedCache = JSON.parse(actCache.value); } catch {}
  try { if (lastSync?.value) parsedSync = JSON.parse(lastSync.value); } catch {}

  return Response.json({
    delivery_enabled: env.DELIVERY_ENABLED === 'true',
    groups: groups.count,
    pending: counts.pending ?? 0,
    failed: counts.failed ?? 0,
    sent: counts.sent ?? 0,
    context_messages: msgCount?.count ?? 0,
    feeds: {
      version: parsedCache?.version ?? null,
      activities: Array.isArray(parsedCache?.activities) ? parsedCache.activities.length : 0,
      redeem_codes: codeCount?.count ?? 0,
      last_sync: parsedSync,
    },
    qq,
  }, { headers: { 'cache-control': 'no-store' } });
}

export default {
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);
      if (request.method === 'GET' && url.pathname === '/health') return Response.json({ ok: true, service: 'maanikki-qq-notifier', assistant: true, feeds: true });
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
    ctx.waitUntil(pollExternalFeeds(env).then(() => dispatch(env)));
    if (Math.floor(controller.scheduledTime / 60000) % 60 === 0) {
      ctx.waitUntil(env.DB.batch([
        env.DB.prepare('DELETE FROM notifications WHERE sent=1 AND created<?').bind(seconds() - 30 * 86400),
        env.DB.prepare("DELETE FROM events WHERE kind NOT IN ('release','legacy_release','activity','redeem') AND created<? AND id NOT IN (SELECT event_id FROM notifications)").bind(seconds() - 7 * 86400),
        env.DB.prepare('DELETE FROM group_messages WHERE created<?').bind(seconds() - 7 * 86400),
      ]));
    }
  },
};
