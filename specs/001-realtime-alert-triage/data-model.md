# Data model

SQLite, one file at `/data/agent.db`. Times are UTC ISO-8601 strings; rendering converts to `Asia/Ho_Chi_Minh`.

## alerts

One row per Alertmanager alert notification (firing or resolved).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| am_fingerprint | TEXT | Alertmanager fingerprint, for traceability |
| status | TEXT | `firing` \| `resolved` |
| labels | TEXT (JSON) | full label set |
| annotations | TEXT (JSON) | redacted |
| starts_at, ends_at | TEXT | from payload |
| received_at | TEXT | when the agent stored it |
| incident_id | INTEGER FK | set by intake |

## incidents

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | shown as `#<id>` in Telegram |
| key | TEXT | fingerprint key from unique while open |
| source | TEXT | `prometheus` \| `loki-app` \| `loki-audit` \| `falco` |
| rule | TEXT | alertname |
| rule_ref | TEXT | production rule number when present |
| namespace, service, route | TEXT | may be empty |
| severity | TEXT | `critical` \| `high` \| `warning` \| `info` |
| protected | INTEGER | 1 skips the gate and is never auto-closed |
| state | TEXT | see state machine |
| occurrences | INTEGER | alerts folded in |
| first_seen, last_seen | TEXT | |
| correlation_id | INTEGER | incidents analysed together share it |
| gate_reason | TEXT | last gate decision, e.g. `rollout catalog-api rev 7` |
| verdict | TEXT (JSON) | [diagnosis-output.md](contracts/diagnosis-output.md) |
| message_id | INTEGER | Telegram message id of the diagnosis |

### Incident state machine

```
new ──gate──► held ──(recheck ok / 10 min)──► queued ──► analysing ──► diagnosed
 │             │                                          │
 │             └─(resolved during hold)──► self_resolved   └─(cost cap / error)──► reported_only
 └─protected──────────────────────────────► queued
diagnosed ──(resolved)──► resolved        any open state ──(resolved)──► resolved
```

`reported_only` is also used for an incident whose namespace is outside `AGENT_NAMESPACES`: no model call, `gate_reason` records why, one short message.

Open states: `new`, `held`, `queued`, `analysing`, `diagnosed`. A firing alert whose key matches a closed incident
opens a new incident (FR-027).

## runs

One row per sub-agent run (US6, kept from the MVP so cost is always known).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| incident_id | INTEGER FK | |
| model | TEXT | terra or sol |
| started_at, finished_at | TEXT | |
| tool_calls | INTEGER | |
| input_tokens, output_tokens | INTEGER | |
| cost_usd | REAL | from config price table |
| outcome | TEXT | `ok` \| `invalid_output` \| `timeout` \| `error` \| `cost_cap` |
| engine | TEXT | `native` (default) \| `holmes`; one row per engine run when `AGENT_ENGINE=both`, and two rows (native then holmes) when `fallback` asked Holmes |
| duration_seconds | REAL | wall-clock time of the run, for the engine comparison |
| verdict | TEXT (JSON) | this run's own verdict ([diagnosis-output.md](contracts/diagnosis-output.md)), so each engine can be graded separately from `incidents.verdict` |

## Validation rules

- `protected = 1` when source is `loki-audit` or `falco`, or the alert carries `protected="true"`.
- `severity` is normalised from the alert label; unknown values become `warning`.
- `verdict.evidence` must be non-empty unless `verdict.label = "unknown"`; `verdict.evidence[].source` is one of `logs|events|rollout|metrics|trace|pod|audit|netpol`.
- `verdict.human_checks` is an optional list of at most 3 strings (≤ 120 chars, redacted); missing means `[]`.
- Every text column that can hold free text passes through the redactor before insert.
