const { trace } = require('@opentelemetry/api');

const SERVICE = process.env.OTEL_SERVICE_NAME || 'orders-api';

function log(level, msg, extra = {}) {
  const ctx = trace.getActiveSpan()?.spanContext();
  const line = { ts: new Date().toISOString(), level, msg, service: SERVICE, trace_id: ctx?.traceId, span_id: ctx?.spanId, ...extra };
  process.stdout.write(JSON.stringify(line) + '\n');
}

module.exports = { log };
