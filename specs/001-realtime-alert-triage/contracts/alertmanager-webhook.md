# Contract: Alertmanager webhook (inbound)

`POST http://alert-agent.alerting.svc:8080/alertmanager`, JSON, Alertmanager webhook format version 4. Already
configured as receiver `alert-agent` with `send_resolved: true` in
`platform/observability/kube-prometheus-stack.yaml`.

Fields read:

| Payload field | Used for |
|---|---|
| `alerts[].status` | `firing` / `resolved` |
| `alerts[].labels.alertname` | incident rule |
| `alerts[].labels.{severity,namespace,service,service_name,http_route,pod,rule_ref,protected,source,user,verb,resource}` | key, severity, protected flag, audit context |
| `alerts[].annotations.{summary,description}` | context for the sub-agent (redacted) |
| `alerts[].startsAt`, `endsAt` | time window for queries |
| `alerts[].fingerprint` | stored, not the key |
| `alerts[].generatorURL` | evidence link |

Responses: `200 {"stored": n}` after the alerts are written to SQLite; `400` on malformed JSON (Alertmanager does
not retry 4xx); `503` when the database is unavailable (Alertmanager retries). The handler never calls the model.

Alerts with `severity="none"` (internal markers such as `ServiceRollingOut`) are stored and used by the gate but
never open an incident.
