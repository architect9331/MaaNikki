// Stores aggregates under a random server-issued reporter ID. Request IPs are never written.
const MAX_BODY = 64 * 1024;
const MAX_ROWS = 500;
const MIN_SUMMARY_TTL_SEC = 6 * 3600;
const MAX_SUMMARY_TTL_SEC = 24 * 3600;
const DAILY_SUMMARY_READ_BUDGET = 400_000;
const REFRESH_LEASE_SEC = 300;
const MEM_SUMMARY_TTL_SEC = 1800;
const STALE_SERVE_TTL_SEC = 60;
const SUMMARY_CACHE_URL = 'https://stats.maanikki.cn/__cache/v2/summary';

let memSummary = { payload: '', expiresAt: 0 };
let summaryInFlight = null;

const text = (value, limit) => typeof value === 'string' && value.length > 0 && value.length <= limit;
const count = (value, limit) => Number.isInteger(value) && value > 0 && value <= limit;
const nowSec = () => Math.floor(Date.now() / 1000);

function validate(body) {
  if (body?.schema !== 1 || !/^[0-9a-f]{32}$/.test(body.batch ?? '') || !text(body.catalog, 32) || !text(body.app, 32)) return false;
  if (!Array.isArray(body.rolls) || !Array.isArray(body.bling) || body.rolls.length > MAX_ROWS || body.bling.length > MAX_ROWS) return false;
  const dig = body.dig ?? [];
  const prefs = body.prefs ?? [];
  if (!Array.isArray(dig) || !Array.isArray(prefs) || dig.length > MAX_ROWS || prefs.length > MAX_ROWS) return false;
  return body.rolls.every((row) => Array.isArray(row) && row.length === 4 && text(row[0], 32) && text(row[1], 8) && text(row[2], 8) && count(row[3], 10000))
    && body.bling.every((row) => Array.isArray(row) && row.length === 2 && count(row[0], 10000) && count(row[1], 100000000))
    && dig.every((row) => Array.isArray(row) && row.length === 2 && text(row[0], 32) && count(row[1], 10000))
    && prefs.every((row) => Array.isArray(row) && row.length === 2 && text(row[0], 64) && text(row[1], 64));
}

async function report(request, env) {
  const raw = await request.text();
  if (raw.length > MAX_BODY) return new Response('too large', { status: 413 });
  let body;
  try { body = JSON.parse(raw); } catch { return new Response('invalid json', { status: 400 }); }
  if (!validate(body)) return new Response('invalid report', { status: 400 });
  const day = new Date().toISOString().slice(0, 10);
  // Unknown or absent IDs get a new one, so clients cannot choose their identity.
  const claimed = /^MaaNikkiID ([0-9a-f]{32})$/.exec(request.headers.get('authorization') ?? '')?.[1];
  const known = claimed && await env.DB.prepare('SELECT id FROM reporters WHERE id = ?').bind(claimed).first();
  const reporter = known ? claimed : crypto.randomUUID().replaceAll('-', '');
  const headers = known ? {} : { 'X-MaaNikki-Set-ID': reporter };
  if (!known) await env.DB.prepare('INSERT INTO reporters (id, day) VALUES (?, ?)').bind(reporter, day).run();
  const first = await env.DB.prepare('INSERT OR IGNORE INTO batches (id, day) VALUES (?, ?)').bind(body.batch, day).run();
  if (!first.meta.changes) return new Response(null, { status: 204, headers });
  const dig = body.dig ?? [];
  const prefs = body.prefs ?? [];
  const statements = [
    ...body.rolls.map(([name, slot, color, value]) => env.DB.prepare(
      `INSERT INTO rolls (reporter, catalog, name, slot, color, count) VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT (reporter, catalog, name, slot, color) DO UPDATE SET count = count + excluded.count`)
      .bind(reporter, body.catalog, name, slot, color, value)),
    ...body.bling.map(([energy, gain]) => env.DB.prepare(
      'INSERT INTO bling_runs (reporter, day, app, energy, gain) VALUES (?, ?, ?, ?, ?)').bind(reporter, day, body.app, energy, gain)),
    ...dig.map(([item, value]) => env.DB.prepare(
      `INSERT INTO dig_targets (reporter, item, count) VALUES (?, ?, ?)
       ON CONFLICT (reporter, item) DO UPDATE SET count = count + excluded.count`)
      .bind(reporter, item, value)),
  ];
  if (prefs.length) {
    const desired = new Map();
    for (const [category, value] of prefs) {
      desired.set(`${category}\0${value}`, [category, value]);
    }
    const existingRows = known
      ? await env.DB.prepare('SELECT category, value FROM reporter_preferences WHERE reporter = ?').bind(reporter).all()
      : { results: [] };
    const kept = new Set();
    for (const row of existingRows.results ?? []) {
      const key = `${row.category}\0${row.value}`;
      if (desired.has(key)) {
        kept.add(key);
      } else {
        statements.push(
          env.DB.prepare('DELETE FROM reporter_preferences WHERE reporter = ? AND category = ? AND value = ?')
            .bind(reporter, row.category, row.value),
        );
      }
    }
    for (const [key, [category, value]] of desired.entries()) {
      if (!kept.has(key)) {
        statements.push(
          env.DB.prepare('INSERT OR IGNORE INTO reporter_preferences (reporter, day, app, category, value) VALUES (?, ?, ?, ?, ?)')
            .bind(reporter, day, body.app, category, value),
        );
      }
    }
  }
  if (statements.length) await env.DB.batch(statements);
  return new Response(null, { status: 204, headers });
}

function buildSummaryResponse(payload, ttlSec = MIN_SUMMARY_TTL_SEC) {
  const safeTtl = Math.max(STALE_SERVE_TTL_SEC, Math.floor(ttlSec));
  return new Response(payload, {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': `public, max-age=${Math.min(3600, safeTtl)}, s-maxage=${safeTtl}, stale-while-revalidate=86400`,
    },
  });
}

function writeEdgeSummary(edgeCache, payload, expiresAt, ctx) {
  if (!edgeCache) return;
  const ttl = Math.max(STALE_SERVE_TTL_SEC, expiresAt - nowSec());
  const cacheKey = new Request(SUMMARY_CACHE_URL, { method: 'GET' });
  const writePromise = edgeCache.put(cacheKey, new Response(payload, {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': `public, max-age=${ttl}, s-maxage=${ttl}`,
      'X-Summary-Expires-At': String(expiresAt),
    },
  })).catch(() => {});
  if (ctx?.waitUntil) ctx.waitUntil(writePromise);
  return writePromise;
}

function computeAdaptiveTtl(rowsRead) {
  if (!Number.isFinite(rowsRead) || rowsRead <= 0) return MIN_SUMMARY_TTL_SEC;
  const scaled = Math.ceil((rowsRead / DAILY_SUMMARY_READ_BUDGET) * 86400);
  return Math.min(MAX_SUMMARY_TTL_SEC, Math.max(MIN_SUMMARY_TTL_SEC, scaled));
}

async function computeSummaryPayload(env) {
  const hasExcluded = Boolean(
    await env.DB.prepare('SELECT 1 AS found FROM reporters WHERE excluded = 1 LIMIT 1').first(),
  );
  const whereActive = hasExcluded
    ? 'WHERE reporter NOT IN (SELECT id FROM reporters WHERE excluded = 1)'
    : '';
  const [rollRows, bling, digTargets, preferences, reporterCounts] = await env.DB.batch([
    env.DB.prepare(`SELECT name, slot, color, SUM(count) AS count FROM rolls ${whereActive} GROUP BY name, slot, color`),
    env.DB.prepare(`SELECT COUNT(*) AS runs, SUM(energy) AS energy, SUM(gain) AS gain FROM bling_runs ${whereActive}`),
    env.DB.prepare(`SELECT item, SUM(count) AS count, COUNT(DISTINCT reporter) AS reporters FROM dig_targets ${whereActive} GROUP BY item ORDER BY count DESC`),
    env.DB.prepare(`SELECT category, value, COUNT(DISTINCT reporter) AS reporters FROM reporter_preferences ${whereActive} GROUP BY category, value ORDER BY category ASC, reporters DESC`),
    env.DB.prepare(`SELECT
        COUNT(DISTINCT CASE WHEN src = 1 THEN reporter END) AS rollReporters,
        COUNT(DISTINCT CASE WHEN src = 2 THEN reporter END) AS digReporters,
        COUNT(DISTINCT CASE WHEN src = 3 THEN reporter END) AS prefReporters,
        COUNT(DISTINCT reporter) AS reporters
      FROM (
        SELECT DISTINCT reporter, 1 AS src FROM rolls
        UNION ALL
        SELECT DISTINCT reporter, 2 AS src FROM dig_targets
        UNION ALL
        SELECT DISTINCT reporter, 3 AS src FROM reporter_preferences
        UNION ALL
        SELECT DISTINCT reporter, 4 AS src FROM bling_runs
      )
      ${whereActive}`),
  ]);
  const slotTotals = new Map();
  const colorTotals = new Map();
  const setColorTotals = new Map();
  for (const row of rollRows.results ?? []) {
    const qty = Number(row.count ?? 0);
    slotTotals.set(row.slot, (slotTotals.get(row.slot) ?? 0) + qty);
    colorTotals.set(row.color, (colorTotals.get(row.color) ?? 0) + qty);
    const scKey = `${row.name}\0${row.color}`;
    const prev = setColorTotals.get(scKey);
    if (prev) prev.count += qty;
    else setColorTotals.set(scKey, { name: row.name, color: row.color, count: qty });
  }
  const slots = Array.from(slotTotals.entries(), ([slot, total]) => ({ slot, count: total }))
    .sort((a, b) => b.count - a.count);
  const colors = Array.from(colorTotals.entries(), ([color, total]) => ({ color, count: total }))
    .sort((a, b) => b.count - a.count);
  const setColors = Array.from(setColorTotals.values())
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : b.count - a.count));
  const counts = reporterCounts.results[0] ?? {};
  const blingRow = bling.results[0] ?? { runs: 0, energy: 0, gain: 0 };
  const rowsRead = [rollRows, bling, digTargets, preferences, reporterCounts]
    .reduce((sum, res) => sum + Number(res?.meta?.rows_read ?? 0), 0);
  const payload = JSON.stringify({
    updatedAt: new Date().toISOString(),
    reporters: counts.reporters ?? 0,
    rollReporters: counts.rollReporters ?? 0,
    digReporters: counts.digReporters ?? 0,
    prefReporters: counts.prefReporters ?? 0,
    colors,
    slots,
    setColors,
    bling: {
      runs: blingRow.runs ?? 0,
      energy: blingRow.energy ?? 0,
      gain: blingRow.gain ?? 0,
    },
    digTargets: digTargets.results,
    preferences: preferences.results,
  });
  return { payload, ttl: computeAdaptiveTtl(rowsRead) };
}

async function refreshSummaryAndPersist(env, edgeCache) {
  const { payload, ttl } = await computeSummaryPayload(env);
  const expiresAt = nowSec() + ttl;
  await env.DB.prepare(
    'INSERT INTO summary_cache (id, payload, expires_at) VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET payload = excluded.payload, expires_at = excluded.expires_at',
  ).bind(payload, expiresAt).run().catch(() => {});
  memSummary = { payload, expiresAt: Math.min(nowSec() + MEM_SUMMARY_TTL_SEC, expiresAt) };
  await writeEdgeSummary(edgeCache, payload, expiresAt, null);
  return { payload, ttl };
}

async function summary(env, ctx) {
  const now = nowSec();
  if (memSummary.payload && memSummary.expiresAt > now) {
    return buildSummaryResponse(memSummary.payload, memSummary.expiresAt - now);
  }
  const cacheKey = new Request(SUMMARY_CACHE_URL, { method: 'GET' });
  let edgeCache = null;
  try {
    edgeCache = caches?.default ?? null;
    if (edgeCache) {
      const hit = await edgeCache.match(cacheKey);
      if (hit) {
        const body = await hit.text();
        const cachedExp = Number.parseInt(hit.headers.get('x-summary-expires-at') ?? '0', 10);
        const remaining = cachedExp > now ? cachedExp - now : MEM_SUMMARY_TTL_SEC;
        memSummary = { payload: body, expiresAt: now + Math.min(MEM_SUMMARY_TTL_SEC, remaining) };
        return buildSummaryResponse(body, remaining);
      }
    }
  } catch {}

  if (!summaryInFlight) {
    summaryInFlight = (async () => {
      const checkNow = nowSec();
      let cachedRow = null;
      try {
        cachedRow = await env.DB.prepare('SELECT payload, expires_at FROM summary_cache WHERE id = 1').first();
      } catch (err) {
        if (memSummary.payload) return { payload: memSummary.payload, ttl: STALE_SERVE_TTL_SEC };
        throw err;
      }

      if (cachedRow?.payload && cachedRow.expires_at > checkNow) {
        const ttlRemaining = Math.max(STALE_SERVE_TTL_SEC, cachedRow.expires_at - checkNow);
        memSummary = { payload: cachedRow.payload, expiresAt: checkNow + Math.min(MEM_SUMMARY_TTL_SEC, ttlRemaining) };
        const p = writeEdgeSummary(edgeCache, cachedRow.payload, cachedRow.expires_at, ctx);
        if (!ctx?.waitUntil && p) await p;
        return { payload: cachedRow.payload, ttl: ttlRemaining };
      }

      if (cachedRow?.payload) {
        // Stale-while-revalidate with an atomic D1 lease so at most one isolate recomputes globally.
        const lease = await env.DB.prepare(
          'UPDATE summary_cache SET expires_at = ? WHERE id = 1 AND expires_at = ?',
        ).bind(checkNow + REFRESH_LEASE_SEC, cachedRow.expires_at).run().catch(() => null);
        if (lease?.meta?.changes) {
          const refreshPromise = refreshSummaryAndPersist(env, edgeCache).catch(() => null);
          if (ctx?.waitUntil) {
            ctx.waitUntil(refreshPromise);
          } else {
            const refreshed = await refreshPromise;
            if (refreshed) return refreshed;
          }
        }
        memSummary = { payload: cachedRow.payload, expiresAt: checkNow + STALE_SERVE_TTL_SEC };
        return { payload: cachedRow.payload, ttl: STALE_SERVE_TTL_SEC };
      }

      return await refreshSummaryAndPersist(env, edgeCache);
    })().finally(() => { summaryInFlight = null; });
  }

  const { payload, ttl } = await summaryInFlight;
  return buildSummaryResponse(payload, ttl);
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (pathname === '/v1/report' && request.method === 'POST') return report(request, env);
    if (pathname === '/v1/summary' && request.method === 'GET') return summary(env, ctx);
    return new Response('not found', { status: 404 });
  },
};
