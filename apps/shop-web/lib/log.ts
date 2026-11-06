import { trace } from '@opentelemetry/api';

const SERVICE = process.env.OTEL_SERVICE_NAME || 'shop-web';

export function log(level: string, msg: string, extra: Record<string, unknown> = {}) {
  const ctx = trace.getActiveSpan()?.spanContext();
  process.stdout.write(
    JSON.stringify({ ts: new Date().toISOString(), level, msg, service: SERVICE, trace_id: ctx?.traceId, span_id: ctx?.spanId, ...extra }) + '\n',
  );
}
