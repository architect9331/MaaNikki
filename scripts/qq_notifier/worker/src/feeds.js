import { clean, sanitizeUrls } from './format.js';

const seconds = () => Math.floor(Date.now() / 1000);
const HOUR_MS = 3600_000;
const DAY_MS = 24 * HOUR_MS;
const FOREVER = 9999999999;

export function parseChinaTimeMs(raw) {
  let s = String(raw ?? '').trim();
  if (!s) return null;
  s = s.replace(
    /^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[T\s/-]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?/,
    (_, y, m, d, hh, mm, ss) => {
      const date = `${y}-${m.padStart(2, '0')}-${d.padStart(2, '0')}`;
      if (hh === undefined) return `${date}T23:59:59`;
      const time = `${hh.padStart(2, '0')}:${mm}${ss !== undefined ? `:${ss}` : ''}`;
      return `${date}T${time}`;
    },
  );
  const hasZone = /Z$|[+-]\d{2}:?\d{2}$/i.test(s);
  const normalized = hasZone ? s : `${s}+08:00`;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : null;
}

export function formatChinaDateTime(ms, includeYear = false) {
  if (!Number.isFinite(ms)) return '未知';
  const d = new Date(ms + 8 * HOUR_MS);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return includeYear ? `${y}-${m}-${day} ${hh}:${mm}` : `${m}-${day} ${hh}:${mm}`;
}

export function formatChinaDate(ms) {
  if (!Number.isFinite(ms)) return '未知';
  const d = new Date(ms + 8 * HOUR_MS);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function chinaHour(ms = Date.now()) {
  return new Date(ms + 8 * HOUR_MS).getUTCHours();
}

export function formatRemaining(diffMs) {
  if (!Number.isFinite(diffMs) || diffMs <= 0) return '已结束';
  const totalMin = Math.floor(diffMs / 60_000);
  const days = Math.floor(totalMin / (24 * 60));
  const hours = Math.floor((totalMin % (24 * 60)) / 60);
  const mins = totalMin % 60;
  if (days >= 1) return `约 ${days} 天 ${hours} 小时`;
  if (hours >= 1) return `约 ${hours} 小时 ${mins} 分钟`;
  return `约 ${Math.max(1, mins)} 分钟`;
}

function buildTrackedActivityItems(snapshot) {
  const items = [];
  const ver = String(snapshot?.version ?? '').trim();
  const verName = String(snapshot?.versionName ?? '').trim();
  const vStart = parseChinaTimeMs(snapshot?.startTime);
  const vEnd = parseChinaTimeMs(snapshot?.endTime);
  if (ver && vStart !== null && vEnd !== null) {
    items.push({
      id: `ver:${ver}`,
      label: verName ? `${ver}版本「${verName}」` : `${ver}版本`,
      kind: '版本周期',
      isVersion: true,
      startMs: vStart,
      endMs: vEnd,
      desc: '',
    });
  }
  for (const act of Array.isArray(snapshot?.activities) ? snapshot.activities : []) {
    const name = String(act?.name ?? '').trim();
    const startMs = parseChinaTimeMs(act?.startTime);
    const endMs = parseChinaTimeMs(act?.endTime);
    if (!name || startMs === null || endMs === null) continue;
    items.push({
      id: `act:${ver || 'unknown'}:${name}:${String(act?.startTime ?? '')}`,
      label: name,
      kind: String(act?.kind ?? '').trim(),
      isVersion: false,
      startMs,
      endMs,
      desc: clean(act?.description ?? '', 90, false),
    });
  }
  return items;
}

export async function fetchSraSnapshot(env) {
  const url = String(env.SRA_ACTIVITY_URL || 'https://starrailassistant.top/api/v1/activity/infinitynikki.json').trim();
  const response = await fetch(url, {
    headers: { 'User-Agent': 'MaaNikki-QQBot/1.0', Accept: 'application/json' },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`SRA HTTP ${response.status}`);
  const data = await response.json();
  if (!data || typeof data !== 'object' || !Array.isArray(data.activities)) {
    throw new Error('Invalid SRA payload');
  }
  const core = {
    version: String(data.version ?? '').trim(),
    versionName: String(data.versionName ?? '').trim(),
    startTime: String(data.startTime ?? '').trim(),
    endTime: String(data.endTime ?? '').trim(),
    activities: data.activities.map(a => ({
      name: String(a?.name ?? '').trim(),
      kind: String(a?.kind ?? '').trim(),
      description: String(a?.description ?? '').trim(),
      startTime: String(a?.startTime ?? '').trim(),
      endTime: String(a?.endTime ?? '').trim(),
    })).filter(a => a.name),
  };
  const nowSec = seconds();
  const prev = await env.DB.prepare("SELECT value, expires_at FROM runtime_state WHERE key='sra-activity-cache'")
    .first().catch(() => null);
  if (prev?.value && Number(prev.expires_at) > nowSec + 5 * 86400) {
    try {
      const parsedPrev = JSON.parse(prev.value);
      const prevCore = {
        version: parsedPrev.version ?? '',
        versionName: parsedPrev.versionName ?? '',
        startTime: parsedPrev.startTime ?? '',
        endTime: parsedPrev.endTime ?? '',
        activities: Array.isArray(parsedPrev.activities) ? parsedPrev.activities : [],
      };
      if (JSON.stringify(prevCore) === JSON.stringify(core)) {
        return { ...core, fetchedAt: parsedPrev.fetchedAt ?? nowSec };
      }
    } catch {}
  }
  const normalized = { ...core, fetchedAt: nowSec };
  await env.DB.prepare(
    "INSERT INTO runtime_state(key,value,expires_at) VALUES('sra-activity-cache',?,?) " +
    'ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at',
  ).bind(JSON.stringify(normalized), nowSec + 7 * 86400).run();
  return normalized;
}

async function getCachedSraSnapshot(env) {
  const cached = await env.DB.prepare("SELECT value FROM runtime_state WHERE key='sra-activity-cache'").first().catch(() => null);
  if (cached?.value) {
    try {
      const parsed = JSON.parse(cached.value);
      if (parsed && Array.isArray(parsed.activities)) return parsed;
    } catch {}
  }
  return await fetchSraSnapshot(env);
}

function finalizeOutbound(text, env) {
  return env.SHOW_LINKS === 'true' ? text : sanitizeUrls(text);
}

export async function syncActivities(env, enqueueFn) {
  const snapshot = await fetchSraSnapshot(env);
  const items = buildTrackedActivityItems(snapshot);
  const nowMs = Date.now();
  const nowSec = seconds();

  const ready = await env.DB.prepare("SELECT 1 AS ok FROM runtime_state WHERE key='activity-baseline-ready'").first();
  if (!ready) {
    const stmts = [];
    for (const item of items) {
      if (item.startMs <= nowMs) {
        stmts.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(`activity:start:${item.id}`, nowSec));
      }
      if (item.endMs - nowMs <= 72 * HOUR_MS) {
        stmts.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(`activity:end72h:${item.id}`, nowSec));
      }
      if (item.endMs - nowMs <= 24 * HOUR_MS) {
        stmts.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(`activity:end24h:${item.id}`, nowSec));
      }
    }
    stmts.push(
      env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('activity-baseline-ready',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at")
        .bind(String(nowSec), FOREVER),
    );
    await env.DB.batch(stmts);
    return { seeded: true, version: snapshot.version, count: items.length, enqueued: 0 };
  }

  const seenRows = await env.DB.prepare("SELECT id FROM events WHERE kind='activity'").all();
  const seen = new Set((seenRows.results || []).map(r => r.id));

  const startedItems = [];
  const startMarkers = [];
  for (const item of items) {
    const startKey = `activity:start:${item.id}`;
    if (nowMs >= item.startMs && nowMs - item.startMs <= 72 * HOUR_MS && item.endMs > nowMs && !seen.has(startKey)) {
      startedItems.push(item);
      startMarkers.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(startKey, nowSec));
      seen.add(startKey);
    }
  }

  let enqueued = 0;
  if (startedItems.length) {
    const lines = ['【无限暖暖 · 新版本/活动开启】'];
    for (const item of startedItems.slice(0, 6)) {
      const prefix = item.isVersion ? '新版本' : (item.kind || '活动');
      lines.push(`• [${prefix}] ${item.label}（截止 ${formatChinaDateTime(item.endMs)}）`);
      if (item.desc) lines.push(`  ${item.desc}`);
    }
    if (startedItems.length > 6) lines.push(`另有 ${startedItems.length - 6} 项活动开启，发送「@我 活动」查看详情。`);
    lines.push('发送「@我 活动」可随时查看当前活动与剩余时间。');
    const msg = finalizeOutbound(lines.join('\n'), env);
    await enqueueFn(env, `activity:batch:start:${snapshot.version || 'v'}:${nowSec}`, 'activity', [msg], null, null, startMarkers);
    enqueued++;
  }

  const bjHour = chinaHour(nowMs);
  if (bjHour >= 9 && bjHour < 22) {
    const endingItems = [];
    const endMarkers = [];
    for (const item of items) {
      if (nowMs < item.startMs || item.endMs <= nowMs) continue;
      const rem = item.endMs - nowMs;
      const key24 = `activity:end24h:${item.id}`;
      const key72 = `activity:end72h:${item.id}`;
      if (rem > HOUR_MS && rem <= 24 * HOUR_MS && !seen.has(key24)) {
        endingItems.push({ ...item, rem, urgent: true });
        endMarkers.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(key24, nowSec));
        endMarkers.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(key72, nowSec));
        seen.add(key24);
        seen.add(key72);
      } else if (rem > 24 * HOUR_MS && rem <= 72 * HOUR_MS && !seen.has(key72)) {
        endingItems.push({ ...item, rem, urgent: false });
        endMarkers.push(env.DB.prepare("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'activity','[]',?,1)").bind(key72, nowSec));
        seen.add(key72);
      }
    }
    if (endingItems.length) {
      endingItems.sort((a, b) => a.rem - b.rem);
      const lines = ['【无限暖暖 · 活动即将结束提醒】'];
      for (const item of endingItems.slice(0, 6)) {
        const tag = item.isVersion ? '当前版本' : (item.urgent ? '最后一天' : '即将结束');
        lines.push(`• [${tag}] ${item.label}：剩余${formatRemaining(item.rem)}（${formatChinaDateTime(item.endMs)} 截止）`);
      }
      lines.push('请记得及时领取活动奖励与完成当期抽取，发送「@我 活动」可随时查看清单。');
      const msg = finalizeOutbound(lines.join('\n'), env);
      await enqueueFn(env, `activity:batch:end:${snapshot.version || 'v'}:${nowSec}`, 'activity', [msg], null, null, endMarkers);
      enqueued++;
    }
  }

  return { seeded: false, version: snapshot.version, count: items.length, enqueued };
}

function decodeHtmlEntities(str) {
  return String(str ?? '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, num) => String.fromCodePoint(Number.parseInt(num, 10)));
}

const CODE_STOPWORDS = new Set([
  '兑换码', '礼包码', '福利码', '无限暖暖', '闪耀暖暖', '官方微博', '直播间',
  '美鸭梨', '设置', '其他', '输入', '暂无', 'none', 'null', 'false', 'true',
]);

const TG_PROMO_POST_RE = /(?:官网|下载|网页|超话|签到|联名|应援车|打卡|跳转|预约|活动进行中|关注官方|微博|直播间|攻略)/;

export function isValidRedeemCode(raw) {
  const s = String(raw ?? '').trim().replace(/^[「『【"'`(<]+|[」』】"'`)>,。，！!；;：:\s]+$/g, '');
  if (s.length < 4 || s.length > 24) return false;
  if (CODE_STOPWORDS.has(s.toLowerCase())) return false;
  if (!/^[A-Za-z0-9\u4e00-\u9fff.]+$/.test(s)) return false;
  if (/^[\d.]+$/.test(s)) return false;
  if (s.includes('.') && /(?<!\d)\.|\.(?!\d)/.test(s)) return false;
  if (/\d{4}年|\d{1,2}月\d{1,2}日|\d{1,2}点\d{0,2}分/.test(s)) return false;
  if (/(?:购买|联名|应援车|打卡|话题|活动|领取|直播间|关注|微博|攻略|前瞻|测试|预约|官网|跳转|下载|网页|前往|进行中|将于|失效|过期|截止|有效期|生效时间|使用方式|点击|打开|输入|邮件|签到|超话|维护|停服|公告|长图|详情|链接|扫码)/.test(s)) return false;
  return true;
}

async function fetchTelegramChannelPosts(env) {
  const channel = String(env.REDEEM_TG_CHANNEL || 'infinity_nikki_weibo_code').trim();
  const url = `https://t.me/s/${encodeURIComponent(channel)}`;
  const response = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0 (compatible; MaaNikki-QQBot/1.0)' },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`TG preview HTTP ${response.status}`);
  const html = await response.text();
  const marker = `data-post="${channel}/`;
  const parts = html.split(marker).slice(1);
  const posts = [];
  for (const part of parts) {
    const idStr = part.split('"', 1)[0];
    const postId = Number.parseInt(idStr, 10);
    if (!Number.isFinite(postId) || postId <= 0) continue;
    const dtMatch = part.match(/datetime="([^"]+)"/);
    const publishedAt = dtMatch ? Math.floor(Date.parse(dtMatch[1]) / 1000) : seconds();
    const textMatch = part.match(/class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/);
    if (!textMatch) continue;
    const fullText = decodeHtmlEntities(
      textMatch[1].replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, ''),
    );
    const codes = [];
    if (!TG_PROMO_POST_RE.test(fullText)) {
      for (const line of fullText.split(/\r?\n/)) {
        const cleaned = line.trim();
        if (isValidRedeemCode(cleaned)) codes.push(cleaned);
      }
    }
    posts.push({ postId, publishedAt: Number.isFinite(publishedAt) ? publishedAt : seconds(), codes });
  }
  return posts;
}

async function fetchBiliWikiCodes() {
  const url = 'https://wiki.biligame.com/wxnn/api.php?action=parse&page=%E5%85%91%E6%8D%A2%E7%A0%81%E5%90%88%E9%9B%86&prop=wikitext&format=json';
  const res = await fetch(url, {
    headers: { 'User-Agent': 'MaaNikki-QQBot/1.0', Accept: 'application/json' },
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`BiliWiki HTTP ${res.status}`);
  const data = await res.json();
  const wikitext = String(data?.parse?.wikitext?.['*'] ?? '');
  if (!wikitext) return [];
  const entries = [];
  const blockRe = /\{\{#subobject:[^|}]*\|([^}]+)\}\}/g;
  let match;
  while ((match = blockRe.exec(wikitext)) !== null) {
    const cleanedBlock = match[1].replace(/\[\[(?:[^|\]]+\|)?([^\]]+)\]\]/g, '$1');
    const fields = {};
    for (const part of cleanedBlock.split('|')) {
      const idx = part.indexOf('=');
      if (idx <= 0) continue;
      fields[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
    }
    if (fields['平台'] !== '国服') continue;
    const code = String(fields['兑换码'] || '').trim();
    if (!isValidRedeemCode(code)) continue;
    const rawReward = String(fields['奖励'] || '').replace(/\s+/g, '').trim();
    const rawExp = String(fields['期限'] || '').trim();
    const expMs = rawExp ? parseChinaTimeMs(rawExp) : null;
    const expiresAt = expMs !== null ? Math.floor(expMs / 1000) : null;
    entries.push({ code, reward: rawReward || null, expiresAt });
  }
  return entries;
}

export async function syncRedeemCodes(env, enqueueFn) {
  const nowSec = seconds();
  const ready = await env.DB.prepare("SELECT 1 AS ok FROM runtime_state WHERE key='redeem-baseline-ready'").first();
  const tgLastRow = await env.DB.prepare("SELECT value FROM runtime_state WHERE key='redeem-tg-last-id'").first();
  const lastTgId = Number.parseInt(tgLastRow?.value || '0', 10) || 0;

  let tgPosts = [];
  let tgError = null;
  try {
    tgPosts = await fetchTelegramChannelPosts(env);
  } catch (err) {
    tgError = err;
  }

  let wikiEntries = [];
  let wikiError = null;
  try {
    wikiEntries = await fetchBiliWikiCodes();
  } catch (err) {
    wikiError = err;
  }

  if (tgError && wikiError) throw wikiError || tgError;

  const maxTgId = tgPosts.reduce((m, p) => Math.max(m, p.postId), lastTgId);
  const wikiByCode = new Map(wikiEntries.map(w => [w.code.toLowerCase(), w]));

  if (!ready) {
    const stmts = [];
    const seenSeed = new Set();
    for (const post of tgPosts) {
      for (const code of post.codes) {
        const key = code.toLowerCase();
        if (seenSeed.has(key)) continue;
        seenSeed.add(key);
        const wiki = wikiByCode.get(key);
        stmts.push(
          env.DB.prepare('INSERT OR IGNORE INTO redeem_codes(code,source,published_at,created,announced,expires_at,reward) VALUES(?,?,?,?,1,?,?)')
            .bind(code, `tg:${post.postId}`, post.publishedAt, nowSec, wiki?.expiresAt ?? null, wiki?.reward ?? null),
        );
      }
    }
    for (const w of wikiEntries) {
      const key = w.code.toLowerCase();
      if (seenSeed.has(key)) continue;
      seenSeed.add(key);
      stmts.push(
        env.DB.prepare('INSERT OR IGNORE INTO redeem_codes(code,source,published_at,created,announced,expires_at,reward) VALUES(?,?,?,?,1,?,?)')
          .bind(w.code, 'biliwiki', nowSec, nowSec, w.expiresAt, w.reward),
      );
    }
    stmts.push(
      env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('redeem-tg-last-id',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at")
        .bind(String(maxTgId), FOREVER),
      env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('redeem-baseline-ready',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at")
        .bind(String(nowSec), FOREVER),
    );
    await env.DB.batch(stmts);
    return { seeded: true, tgMax: maxTgId, codesCount: seenSeed.size, enqueued: 0, deferSeconds: 0 };
  }

  // Defer 2 minutes if a brand-new Telegram code post just landed (<120s),
  // so multi-code livestream bursts merge cleanly into one QQ message.
  const unseenTgPosts = tgPosts.filter(p => p.postId > lastTgId);
  if (unseenTgPosts.length) {
    const latestPublished = unseenTgPosts.reduce((m, p) => Math.max(m, p.publishedAt), 0);
    if (nowSec - latestPublished < 120) {
      return { seeded: false, deferred: true, deferSeconds: 120, enqueued: 0 };
    }
  }

  const candidates = [];
  for (const post of unseenTgPosts) {
    for (const code of post.codes) {
      const wiki = wikiByCode.get(code.toLowerCase());
      candidates.push({ code, source: `tg:${post.postId}`, publishedAt: post.publishedAt, expiresAt: wiki?.expiresAt ?? null, reward: wiki?.reward ?? null });
    }
  }
  for (const w of wikiEntries) {
    if (w.expiresAt === null || w.expiresAt > nowSec) {
      candidates.push({ code: w.code, source: 'biliwiki', publishedAt: nowSec, expiresAt: w.expiresAt, reward: w.reward });
    }
  }

  const existingRows = await env.DB.prepare('SELECT code, expires_at, reward FROM redeem_codes').all();
  const knownMap = new Map((existingRows.results || []).map(r => [r.code.toLowerCase(), r]));
  const freshEntries = [];
  const enrichStmts = [];
  for (const w of wikiEntries) {
    const prev = knownMap.get(w.code.toLowerCase());
    if (prev && ((w.expiresAt !== null && prev.expires_at !== w.expiresAt) || (w.reward && prev.reward !== w.reward))) {
      enrichStmts.push(
        env.DB.prepare('UPDATE redeem_codes SET expires_at=COALESCE(?,expires_at), reward=COALESCE(?,reward) WHERE code=?')
          .bind(w.expiresAt, w.reward, prev.code),
      );
    } else if (!prev && w.expiresAt !== null && w.expiresAt <= nowSec) {
      // Silently archive already-expired historical BiliWiki codes without notifying.
      knownMap.set(w.code.toLowerCase(), { code: w.code });
      enrichStmts.push(
        env.DB.prepare('INSERT OR IGNORE INTO redeem_codes(code,source,published_at,created,announced,expires_at,reward) VALUES(?,?,?,?,1,?,?)')
          .bind(w.code, 'biliwiki', w.expiresAt, nowSec, w.expiresAt, w.reward),
      );
    }
  }
  for (const c of candidates) {
    const key = c.code.toLowerCase();
    if (knownMap.has(key)) continue;
    knownMap.set(key, { code: c.code });
    freshEntries.push(c);
  }

  const stateUpdates = [...enrichStmts];
  if (maxTgId > lastTgId) {
    stateUpdates.push(
      env.DB.prepare("INSERT INTO runtime_state(key,value,expires_at) VALUES('redeem-tg-last-id',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at")
        .bind(String(maxTgId), FOREVER),
    );
  }

  let enqueued = 0;
  if (freshEntries.length) {
    const insertStmts = freshEntries.map(e =>
      env.DB.prepare('INSERT OR IGNORE INTO redeem_codes(code,source,published_at,created,announced,expires_at,reward) VALUES(?,?,?,?,1,?,?)')
        .bind(e.code, e.source, e.publishedAt, nowSec, e.expiresAt ?? null, e.reward ?? null),
    );
    const lines = [
      '【无限暖暖 · 新兑换码】',
      ...freshEntries.map(e => `• ${e.code}${e.reward ? `（${e.reward}）` : ''}`),
      '兑换方式：游戏内左上角「美鸭梨 → 设置 → 其他 → 兑换码」输入领取，也可在 MaaNikki 勾选「兑换码领取」自动兑换，注意时效及时兑换。',
    ];
    const eventKey = `redeem:${freshEntries.map(e => e.code).join(',')}`;
    await enqueueFn(env, eventKey, 'redeem', [finalizeOutbound(lines.join('\n'), env)], null, null, [...insertStmts, ...stateUpdates]);
    enqueued = 1;
  } else if (stateUpdates.length) {
    await env.DB.batch(stateUpdates);
  }

  return {
    seeded: false,
    tgMax: maxTgId,
    wikiCount: wikiEntries.length,
    newCodes: freshEntries.length,
    enriched: enrichStmts.length,
    changed: freshEntries.length > 0 || enrichStmts.length > 0,
    enqueued,
    deferSeconds: 0,
  };
}

export async function formatActivitiesReply(env) {
  let snapshot;
  try {
    snapshot = await getCachedSraSnapshot(env);
  } catch {
    return '暂时无法获取活动数据，请稍后再试。';
  }
  const items = buildTrackedActivityItems(snapshot);
  const nowMs = Date.now();
  const verItem = items.find(i => i.isVersion);
  const actItems = items.filter(i => !i.isVersion);
  const active = actItems.filter(i => i.startMs <= nowMs && i.endMs > nowMs);
  const upcoming = actItems.filter(i => i.startMs > nowMs);

  const lines = ['【无限暖暖 · 当前活动日历】'];
  if (verItem) {
    const status = verItem.endMs > nowMs ? `剩余${formatRemaining(verItem.endMs - nowMs)}` : '已结束（待更新）';
    lines.push(`当前版本：${verItem.label}（${formatChinaDateTime(verItem.startMs)} ~ ${formatChinaDateTime(verItem.endMs)}，${status}）`);
  }
  if (active.length) {
    lines.push('进行中活动：');
    for (const a of active.slice(0, 6)) {
      const kindTag = a.kind ? `[${a.kind}] ` : '';
      lines.push(`• ${kindTag}${a.label}：剩余${formatRemaining(a.endMs - nowMs)}（${formatChinaDateTime(a.endMs)} 截止）`);
    }
  } else {
    lines.push('当前暂无记录中的进行中限时活动（等待新版本公告同步）。');
  }
  if (upcoming.length) {
    lines.push('即将开启：');
    for (const u of upcoming.slice(0, 3)) {
      lines.push(`• ${u.label}：${formatChinaDateTime(u.startMs)} 开启`);
    }
  }
  lines.push('新版本/新活动开启及临期前，本群会自动推送提醒。');
  return finalizeOutbound(lines.join('\n'), env);
}

export async function formatRedeemCodesReply(env) {
  const nowSec = seconds();
  const rows = await env.DB.prepare(
    'SELECT code, published_at, expires_at, reward FROM redeem_codes ' +
    'WHERE expires_at IS NULL OR expires_at>? ORDER BY published_at DESC, created DESC LIMIT 8',
  ).bind(nowSec).all().catch(() => ({ results: [] }));
  let list = rows.results || [];
  let showingExpiredFallback = false;
  if (!list.length) {
    const fallback = await env.DB.prepare('SELECT code, published_at, expires_at, reward FROM redeem_codes ORDER BY published_at DESC, created DESC LIMIT 6')
      .all().catch(() => ({ results: [] }));
    list = fallback.results || [];
    showingExpiredFallback = true;
  }
  if (!list.length) {
    return '暂未收录近期兑换码；监控服务运行中，收录到新兑换码后会第一时间在本群推送。';
  }
  const newestAt = Number(list[0]?.published_at || 0);
  const lines = [showingExpiredFallback ? '【无限暖暖 · 近期兑换码记录】' : '【无限暖暖 · 当前可用及近期兑换码】'];
  for (const r of list.slice(0, 6)) {
    const expText = r.expires_at ? `，截止 ${formatChinaDate(Number(r.expires_at) * 1000)}` : `，${formatChinaDate(Number(r.published_at) * 1000)}`;
    const rewardText = r.reward ? `｜${r.reward}` : '';
    lines.push(`• ${r.code}（${expText.replace(/^，/, '')}${rewardText}）`);
  }
  if (showingExpiredFallback || (newestAt && nowSec - newestAt > 21 * 86400 && !list.some(r => r.expires_at && Number(r.expires_at) > nowSec))) {
    lines.push('注：近期暂无新码，部分早期兑换码可能已过期；有新码发布时本群会自动推送。');
  }
  lines.push('兑换方式：游戏内左上角「美鸭梨 → 设置 → 其他 → 兑换码」（也可在 MaaNikki 勾选「兑换码领取」自动兑换）。');
  return finalizeOutbound(lines.join('\n'), env);
}

export async function listPublicRedeemCodes(env) {
  const rows = await env.DB.prepare(
    'SELECT code, source, published_at, expires_at, reward FROM redeem_codes ORDER BY published_at DESC, created DESC LIMIT 40',
  ).all().catch(() => ({ results: [] }));
  return {
    schema_version: 1,
    updated_at: seconds(),
    codes: (rows.results || []).map(r => ({
      code: r.code,
      source: r.source,
      published_at: Number(r.published_at || 0),
      expires_at: r.expires_at !== null && r.expires_at !== undefined ? Number(r.expires_at) : null,
      reward: r.reward || null,
    })),
  };
}

export async function getFeedsContextSummary(env) {
  const parts = [];
  try {
    const cached = await env.DB.prepare("SELECT value FROM runtime_state WHERE key='sra-activity-cache'").first();
    if (cached?.value) {
      const snap = JSON.parse(cached.value);
      const items = buildTrackedActivityItems(snap);
      const nowMs = Date.now();
      const ver = items.find(i => i.isVersion);
      const active = items.filter(i => !i.isVersion && i.endMs > nowMs);
      const lines = ['【无限暖暖当前版本与活动】'];
      if (ver) {
        lines.push(`${ver.label}（${formatChinaDateTime(ver.startMs)} ~ ${formatChinaDateTime(ver.endMs)}，${ver.endMs > nowMs ? `剩余${formatRemaining(ver.endMs - nowMs)}` : '已结束'}）`);
      }
      for (const a of active.slice(0, 4)) {
        lines.push(`• ${a.label}（${formatChinaDateTime(a.startMs)} ~ ${formatChinaDateTime(a.endMs)}）`);
      }
      parts.push(lines.join('\n'));
    }
  } catch {}
  try {
    const nowSec = seconds();
    const rows = await env.DB.prepare(
      'SELECT code, published_at, expires_at FROM redeem_codes WHERE expires_at IS NULL OR expires_at>? ORDER BY published_at DESC, created DESC LIMIT 4',
    ).bind(nowSec).all();
    const list = rows.results || [];
    if (list.length) {
      parts.push('【有效及近期兑换码】\n' + list.map(r => `${r.code} (${r.expires_at ? '截止' + formatChinaDate(Number(r.expires_at) * 1000) : formatChinaDate(Number(r.published_at) * 1000)})`).join('、'));
    }
  } catch {}
  return parts.join('\n\n').slice(0, 500);
}
