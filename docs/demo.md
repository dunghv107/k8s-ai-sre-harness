# Demo walkthrough (10 minutes)

A live tour of the alert pipeline for an interviewer. Every scenario is one command of `scripts/demo.sh`; the alert
lands in a real Telegram group. Rule details are in [siem.md](siem.md), the app routes in [contract.md](contract.md).

## Prerequisites

- Cluster up and healthy: `kubectl get pods -A` shows everything `Running` (`bash scripts/up.sh`, `platform.sh`, `deploy-apps.sh`).
- `.env` filled with the Telegram bot token and chat id; the Telegram group open on a second screen.
- Git Bash (Windows) or any bash with `kubectl`; `scripts/demo.sh shell` needs a real terminal (mintty or Windows Terminal), not an IDE task runner.
- Three port-forwards in separate terminals (Alertmanager and Grafana are for the audience, nothing in `demo.sh` needs them):

```bash
kubectl -n observability port-forward svc/kps-alertmanager 9093:9093   # http://localhost:9093
kubectl -n observability port-forward svc/kps-grafana 3001:80          # http://localhost:3001, admin / GRAFANA_ADMIN_PASSWORD
kubectl -n kube-system port-forward svc/hubble-ui 12000:80             # http://localhost:12000, namespace "shop"
```

Each port-forward is itself an audit event (`AuditExecByUser`, a warning, "system:admin used portforward"). Start them
before the demo and let them age out, or point it out as a bonus: the SIEM sees the presenter too.

- Quiet baseline: run `bash scripts/demo.sh status` and wait until it prints nothing but the header (alerts resolve a few minutes after their cause stops).

## Timings you can rely on

Measured against the live cluster (Prometheus and Loki rules evaluate every 1 m, Alertmanager waits 30 s before the first message).
"In Alertmanager" is when the alert shows in `status` or on :9093; add up to 30 s for the Telegram message.

| Scenario | Command | Alerts | In Alertmanager | Telegram |
|---|---|---|---|---|
| Deliberate 500 | `5xx` (3 min of traffic) | `Http5xxRate` catalog-api + shop-web, `AppErrorLogs` | 2:15 and 2:35 after start | yes |
| Error rate | `error-rate` (4 min of orders) | `Http5xxRate` orders-api, `AppErrorLogs` | about 2:30 | yes |
| Crash loop | `crash` (about 1:40 to send 4 crashes) | `PodRestartingFrequently` | 1 to 3 min after the last crash | yes |
| OOM | `oom` | `PodOOMKilled` | about 1:15 | yes |
| Network policy | `netpol` (3:20 of orders) | `AuditNetworkPolicyChange` x2, `Http5xxRate` orders-api, `AppErrorLogs`, `HubblePolicyDrops`, later `PodRestartingFrequently` (liveness needs the DB) | audit under 1:40, 5xx 2:30, drops about 3:00, restarts about 6 min | yes |
| Rollout | `rollout` | `ServiceRollingOut` (silent), catalog-api `Http5xxRate` and `AppErrorLogs` suppressed | marker after about 1:15, 5xx suppressed at 2:15 | catalog-api: none (shop-web 5xx still pages) |
| Shell | `shell` | `AuditExecByUser`, `FalcoTerminalShell` | audit under 2 min, Falco 1 to 2 min | yes |
| Secrets | `secret` | `AuditSecretsListByServiceAccount` (critical), `AuditRBACChangeByAdmin` | about 1:30 | yes |

Alertmanager's own delivery counter (`alertmanager_notifications_total{integration="telegram"}`) went 13 to 58 over the whole test run with every
`alertmanager_notifications_failed_total` series at 0.

## Script (10 minutes)

Run the scenarios that need minutes of traffic first, so alerts arrive while you talk. Between runs, keep `status` handy.

### 0:00 The picture (1 min)

Show the README diagram: app metrics, logs and traces plus the API audit log and Falco feed Prometheus and Loki; both rulers
feed Alertmanager; Alertmanager posts to Telegram. Say one thing: "the same flow runs in production at DFM, this is the laptop version."

### 1:00 Business 5xx and log alert (3 min)

```bash
bash scripts/demo.sh 5xx
```

No browser is needed: the script drives shop-web itself (`GET /api/products/4`, the product with no variant, a real bug).
While it runs, in a second terminal start the next scenario that does not touch catalog-api:

```bash
bash scripts/demo.sh secret
```

Show in Telegram:

- `🚨 🛡️ CRITICAL · loki-audit`, "ServiceAccount system:serviceaccount:demo-siem:probe list Secrets in namespace 'demo-siem': the token in a pod may be stolen" (about 1:30).
- Around 3 min: `CRITICAL` "5xx responses from catalog-api on /api/products/{product_id}" and the same for shop-web, then a `WARNING` `AppErrorLogs` ("130 error log lines in 2m").

Show in Grafana (Explore): Prometheus `sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[2m]))`,
then Loki `{app="catalog-api"} | json | level="error"`; click a `trace_id` to jump to the Tempo trace. The alert, the log line and the trace are one click apart.

### 4:00 Runtime and audit security (2 min)

In a real terminal:

```bash
bash scripts/demo.sh shell
```

Expect `WARNING` `AuditExecByUser` ("system:admin used exec on pod shop/orders-api-...") and `WARNING` `FalcoTerminalShell`
("Falco: terminal shell in container", the command line is in the message). Two independent sensors, one action: the API audit log and the kernel.

### 6:00 The video's story: a network policy breaks orders (3 min)

```bash
bash scripts/demo.sh netpol
```

The script applies a policy as a non-admin identity (`--as=demo-operator`), which removes orders-api from the postgres allow-list.
Walk through the alerts as they arrive: `AuditNetworkPolicyChange` ("demo-operator patch networkpolicies shop/postgres-ingress", under 2 min) first,
then orders-api `Http5xxRate` and `AppErrorLogs`, then `HubblePolicyDrops` ("Traffic denied by NetworkPolicy"). In Hubble UI, namespace `shop`, the
orders-api to postgres flow is red (dropped, policy denied). Close the loop:

```bash
bash scripts/demo.sh netpol-undo
```

### 9:00 Inhibition and wrap-up (1 min)

Do not run this live unless time is left; it needs 2.5 minutes. Explain it with the Alertmanager UI instead: open http://localhost:9093, tick "Inhibited",
and show a rollout-suppressed alert (run `bash scripts/demo.sh rollout` before the call and keep it for reference):
`ServiceRollingOut` is a silent marker that mutes `Http5xxRate`, `PodRestartingFrequently`, `DeploymentReplicasMissing` and `AppErrorLogs` for the same
service while it deploys. `bash scripts/demo.sh status` shows the same with `suppressed` and the `inhibitedBy` id.

Finish with `bash scripts/demo.sh reset` (undoes the policy, deletes `demo-siem`, restarts the four deployments to clear fault state), and mention what comes next: the AI triage agent
that reads these alerts, correlates them and posts a diagnosis in the same chat.

## Spare scenarios (not in the 10 minutes)

| Command | Why |
|---|---|
| `error-rate` | Failure at a chosen rate on orders-api, which is the realistic partial outage. |
| `crash` | Shows `PodRestartingFrequently` (3 restarts in 10 min). |
| `oom` | Shows `PodOOMKilled` (the pod restarts by itself, the alert says why). |

## Things worth knowing before you present

- The `AuditExecByUser` alerts about port-forward and exec are real and expected; `status` and the shop-web traffic use `kubectl proxy`/`get --raw`, so they add none.
- `crash`, `oom` and `error-rate` talk to one pod through a port-forward (the backend NetworkPolicies only admit shop-web, and the API-server proxy would be dropped and itself raise `HubblePolicyDrops`).
- After `netpol`, orders-api liveness fails (health checks the DB) and the pods restart; `reset` or `netpol-undo` brings them back.
- Alerts resolve a few minutes after the cause stops, and Telegram posts a "resolved" message; repeat the same scenario only after `status` is quiet.
- Tempo can be OOMKilled on a small Docker Desktop budget (it happened once during testing); that raises a genuine `PodOOMKilled` for `tempo-0`, unrelated to the demo.
