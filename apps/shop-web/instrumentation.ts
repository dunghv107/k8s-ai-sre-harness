import { registerOTel } from '@vercel/otel';

export function register() {
  try {
    registerOTel({
      serviceName: process.env.OTEL_SERVICE_NAME || 'shop-web',
      // by default traceparent is only sent to same-origin URLs; the server only calls backends, so allow all
      instrumentationConfig: { fetch: { propagateContextUrls: [/.*/] } },
    });
  } catch (err) {
    // tracing must never stop the app from starting
    console.error(JSON.stringify({ level: 'error', msg: 'otel init failed', error: String(err) }));
  }
}
