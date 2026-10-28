import { env } from 'cloudflare:workers';

export default {
  async fetch(request) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD' } });
    }
    const key = new URL(request.url).pathname.slice(1);
    if (key !== 'releases.json' && !/^v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?\/MaaNikki-[A-Za-z0-9_.-]+\.(?:zip|txt)$/.test(key)) {
      return new Response('Not found', { status: 404 });
    }
    // One storage read per invocation keeps the Workers Free daily request cap
    // below R2's monthly read allowance, including requests for missing files.
    const object = await env.RELEASES.get(key, { onlyIf: request.headers });
    if (!object) return new Response('Not found', { status: 404 });
    const headers = new Headers({
      'ETag': object.httpEtag,
      'Cache-Control': key === 'releases.json' ? 'no-store' : 'public, max-age=31536000, immutable',
      'X-Content-Type-Options': 'nosniff',
    });
    object.writeHttpMetadata(headers);
    if (!('body' in object)) {
      return new Response(null, { status: 304, headers });
    }
    headers.set('Content-Length', String(object.size));
    if (request.method === 'HEAD') {
      await object.body.cancel();
      return new Response(null, { headers });
    }
    return new Response(object.body, { headers });
  },
} satisfies ExportedHandler;
