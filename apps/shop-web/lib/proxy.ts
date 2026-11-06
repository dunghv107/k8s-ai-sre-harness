import { log } from './log';

export const CATALOG_URL = process.env.CATALOG_URL || 'http://catalog-api:8080';
export const ORDERS_URL = process.env.ORDERS_URL || 'http://orders-api:8080';

// forwards status and body untouched so the page shows the real backend answer
export async function proxy(route: string, url: string, init?: RequestInit): Promise<Response> {
  const start = Date.now();
  const method = init?.method || 'GET';
  try {
    const res = await fetch(url, { ...init, cache: 'no-store', signal: AbortSignal.timeout(10000) });
    const text = await res.text();
    const failed = res.status >= 500;
    log(failed ? 'error' : 'info', 'request', {
      method, route, status: res.status, duration_ms: Date.now() - start,
      ...(failed && { error: `upstream ${url} returned ${res.status}`, stack: new Error().stack }),
    });
    return new Response(text, { status: res.status, headers: { 'content-type': res.headers.get('content-type') || 'application/json' } });
  } catch (err) {
    const e = err as Error;
    log('error', 'request', { method, route, status: 502, duration_ms: Date.now() - start, error: e.message, stack: e.stack });
    return Response.json({ error: `upstream unreachable: ${e.message}` }, { status: 502 });
  }
}
