// Stores aggregates under a random server-issued reporter ID. Request IPs are never written.
const MAX_BODY = 64 * 1024;
const MAX_ROWS = 500;
const text = (value, limit) => typeof value === 'string' && value.length > 0 && value.length <= limit;
const count = (value, limit) => Number.isInteger(value) && value > 0 && value <= limit;

function validate(body) {
  if (body?.schema !== 1 || !/^[0-9a-f]{32}$/.test(body.batch ?? '') || !text(body.catalog, 32) || !text(body.app, 32)) return false;
  if (!Array.isArray(body.rolls) || !Array.isArray(body.bling) || body.rolls.length > MAX_ROWS || body.bling.length > MAX_ROWS) return false;
  return body.rolls.every((row) => Array.isArray(row) && row.length === 4 && text(row[0], 32) && text(row[1], 8) && text(row[2], 8) && count(row[3], 10000))
    && body.bling.every((row) => Array.isArray(row) && row.length === 2 && count(row[0], 10000) && count(row[1], 100000000));
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
  const statements = [
    ...body.rolls.map(([name, slot, color, value]) => env.DB.prepare(
      `INSERT INTO rolls (reporter, catalog, name, slot, color, count) VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT (reporter, catalog, name, slot, color) DO UPDATE SET count = count + excluded.count`)
      .bind(reporter, body.catalog, name, slot, color, value)),
    ...body.bling.map(([energy, gain]) => env.DB.prepare(
      'INSERT INTO bling_runs (reporter, day, app, energy, gain) VALUES (?, ?, ?, ?, ?)').bind(reporter, day, body.app, energy, gain)),
  ];
  if (statements.length) await env.DB.batch(statements);
  return new Response(null, { status: 204, headers });
}

async function summary(env) {
  const colors = await env.DB.prepare(
    `SELECT color, SUM(count) AS count FROM rolls WHERE reporter NOT IN (SELECT id FROM reporters WHERE excluded = 1)
     GROUP BY color ORDER BY count DESC`).all();
  const bling = await env.DB.prepare(
    `SELECT COUNT(*) AS runs, SUM(energy) AS energy, SUM(gain) AS gain FROM bling_runs
     WHERE reporter NOT IN (SELECT id FROM reporters WHERE excluded = 1)`).first();
  const reporters = await env.DB.prepare('SELECT COUNT(*) AS count FROM reporters WHERE excluded = 0').first();
  return Response.json({ reporters: reporters.count, colors: colors.results, bling });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === '/v1/report' && request.method === 'POST') return report(request, env);
    if (pathname === '/v1/summary' && request.method === 'GET') return summary(env);
    return new Response('not found', { status: 404 });
  },
};
