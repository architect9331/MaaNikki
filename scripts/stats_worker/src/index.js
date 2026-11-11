// Stores aggregates under a random server-issued reporter ID. Request IPs are never written.
const MAX_BODY = 64 * 1024;
const MAX_ROWS = 500;
const text = (value, limit) => typeof value === 'string' && value.length > 0 && value.length <= limit;
const count = (value, limit) => Number.isInteger(value) && value > 0 && value <= limit;

function validate(body) {
  if (body?.schema !== 1 || !/^[0-9a-f]{32}$/.test(body.batch ?? '') || !text(body.catalog, 32) || !text(body.app, 32)) return false;
  if (!Array.isArray(body.rolls) || !Array.isArray(body.bling) || body.rolls.length > MAX_ROWS || body.bling.length > MAX_ROWS) return false;
  const dig = body.dig ?? [];
  const prefs = body.prefs ?? [];
  if (!Array.isArray(dig) || !Array.isArray(prefs) || dig.length > MAX_ROWS || prefs.length > MAX_ROWS) return false;
  return body.rolls.every((row) => Array.isArray(row) && row.length === 4 && text(row[0], 32) && text(row[1], 8) && text(row[2], 8) && count(row[3], 10000))
    && body.bling.every((row) => Array.isArray(row) && row.length === 2 && count(row[0], 10000) && count(row[1], 100000000))
    && dig.every((row) => Array.isArray(row) && row.length === 2 && text(row[0], 32) && count(row[1], 10000))
    && prefs.every((row) => Array.isArray(row) && row.length === 2 && text(row[0], 48) && text(row[1], 64));
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
    ...(prefs.length
      ? [
          env.DB.prepare('DELETE FROM reporter_preferences WHERE reporter = ?').bind(reporter),
          ...prefs.map(([category, value]) => env.DB.prepare(
            'INSERT OR IGNORE INTO reporter_preferences (reporter, day, app, category, value) VALUES (?, ?, ?, ?, ?)')
            .bind(reporter, day, body.app, category, value)),
        ]
      : []),
  ];
  if (statements.length) await env.DB.batch(statements);
  return new Response(null, { status: 204, headers });
}

async function summary(env) {
  const active = 'reporter NOT IN (SELECT id FROM reporters WHERE excluded = 1)';
  const [colors, slots, setColors, rollReporters, bling] = await env.DB.batch([
    env.DB.prepare(`SELECT color, SUM(count) AS count FROM rolls WHERE ${active} GROUP BY color ORDER BY count DESC`),
    env.DB.prepare(`SELECT slot, SUM(count) AS count FROM rolls WHERE ${active} GROUP BY slot ORDER BY count DESC`),
    env.DB.prepare(`SELECT name, color, SUM(count) AS count FROM rolls WHERE ${active} GROUP BY name, color ORDER BY name ASC, count DESC`),
    env.DB.prepare(`SELECT COUNT(DISTINCT reporter) AS count FROM rolls WHERE ${active}`),
    env.DB.prepare(`SELECT COUNT(*) AS runs, SUM(energy) AS energy, SUM(gain) AS gain FROM bling_runs WHERE ${active}`),
  ]);
  const digTargets = await env.DB.prepare(
    `SELECT item, SUM(count) AS count, COUNT(DISTINCT reporter) AS reporters FROM dig_targets
     WHERE ${active} GROUP BY item ORDER BY count DESC`).all().catch(() => ({ results: [] }));
  const digReporters = await env.DB.prepare(
    `SELECT COUNT(DISTINCT reporter) AS count FROM dig_targets WHERE ${active}`).first().catch(() => ({ count: 0 }));
  const preferences = await env.DB.prepare(
    `SELECT category, value, COUNT(DISTINCT reporter) AS reporters FROM reporter_preferences
     WHERE ${active} GROUP BY category, value ORDER BY category ASC, reporters DESC`).all().catch(() => ({ results: [] }));
  const prefReporters = await env.DB.prepare(
    `SELECT COUNT(DISTINCT reporter) AS count FROM reporter_preferences WHERE ${active}`).first().catch(() => ({ count: 0 }));
  // A lost response can orphan an issued ID, so count only IDs that hold data.
  const reporters = await env.DB.prepare(
    `SELECT COUNT(*) AS count FROM reporters WHERE excluded = 0
     AND (id IN (SELECT reporter FROM rolls) OR id IN (SELECT reporter FROM bling_runs)
          OR id IN (SELECT reporter FROM dig_targets) OR id IN (SELECT reporter FROM reporter_preferences))`).first()
    .catch(() => env.DB.prepare(
      `SELECT COUNT(*) AS count FROM reporters WHERE excluded = 0
       AND (id IN (SELECT reporter FROM rolls) OR id IN (SELECT reporter FROM bling_runs))`).first());
  return Response.json(
    {
      updatedAt: new Date().toISOString(),
      reporters: reporters?.count ?? 0,
      rollReporters: rollReporters.results[0]?.count ?? 0,
      digReporters: digReporters?.count ?? 0,
      prefReporters: prefReporters?.count ?? 0,
      colors: colors.results,
      slots: slots.results,
      setColors: setColors.results,
      bling: bling.results[0] ?? { runs: 0, energy: 0, gain: 0 },
      digTargets: digTargets.results,
      preferences: preferences.results,
    },
    {
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'public, max-age=120',
      },
    },
  );
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === '/v1/report' && request.method === 'POST') return report(request, env);
    if (pathname === '/v1/summary' && request.method === 'GET') return summary(env);
    return new Response('not found', { status: 404 });
  },
};
