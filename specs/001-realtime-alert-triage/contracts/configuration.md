# Contract: configuration

Environment variables (names match `.env.example`; secrets come from Secret `alert-agent`, the rest from the
Deployment).

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | — (Secret) | model access |
| `OPENAI_MODEL` | `gpt-6.1-terra` | default sub-agent model |
| `OPENAI_MODEL_ESCALATE` | `gpt-6.1-sol` | protected (security) incidents |
| `AGENT_DAILY_COST_CAP_USD` | `2` | per local day |
| `TELEGRAM_BOT_TOKEN` | — (Secret) | bot |
| `TELEGRAM_ALERT_CHAT_ID` | — (Secret) | group |
| `TELEGRAM_ALLOWED_USER_IDS` | empty | command allow-list (US4) |
| `AGENT_TIMEZONE` | `Asia/Ho_Chi_Minh` | rendering, cost day, 08:30 summary |
| `AGENT_BATCH_WINDOW_SECONDS` | `60` | storm batching |
| `AGENT_MAX_PARALLEL_SUBAGENTS` | `5` | concurrency |
| `AGENT_SUBAGENT_TIMEOUT_SECONDS` | `300` | per run |
| `AGENT_DEMO_MODE` | `false` | true: no OpenAI/Telegram calls, stub verdict, log only |
| `AGENT_NAMESPACES` | `shop` | namespaces tools may read |
| `AGENT_GATE_POD_AGE_SECONDS` | `180` | young-pod hold |
| `AGENT_GATE_MIN_ERRORS` | `3` | threshold per 120 s |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | in-cluster service URLs | telemetry |
| `AGENT_ENGINE` | `fallback` | investigation engine: `fallback` (native tool loop first; when its verdict is `unknown` or confidence `low`, HolmesGPT is asked once; one final message tagged `[native]` or `[holmes]`, both runs recorded in `runs`), `native` (OpenAI tool loop only), `holmes` (HolmesGPT only) or `both` (benchmark: race them, post both tagged; the incident's verdict is the native one) |
| `HOLMES_URL` | `http://holmes-holmes.alerting.svc` | HolmesGPT service, used when the engine is `fallback`, `holmes` or `both` |
| `HOLMES_API_KEY` | empty (Secret `alert-agent`) | sent as `X-API-Key`; same value as in Secret `holmes-secrets` |
