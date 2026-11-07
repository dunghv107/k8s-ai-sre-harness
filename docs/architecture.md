# Lab architecture and alert flow

This document describes the target state defined by [spec 002](../specs/002-homelab-platform/spec.md)
(platform) and [spec 001](../specs/001-realtime-alert-triage/spec.md) (alert agent).

## 1. Host

| Item | Value |
|---|---|
| Host | Windows 11 workstation, 24 GB RAM, 12 threads |
| Runtime | Docker Desktop on WSL2, WSL memory capped at ~12 GB |
| Cluster | k3d: 1 server + 2 agent nodes (k3s, CNCF-conformant Kubernetes) |
| Budget | ≤ 10 GB RAM for the whole production-replica lab in steady state |
| Access | `localhost` only, TLS; no public domain, no inbound port from the Internet |

The host must stay awake for overnight processing. When it was off, the 08:30 summary states the gap.

## 2. Platform layers

| Layer | Component | Role |
|---|---|---|
| GitOps | Argo CD | Syncs everything under `platform/` and `apps/` from this repo. Only the bootstrap runs by hand |
| Ingress | Traefik + Gateway API | One shared Gateway per environment namespace; each app adds its own HTTPRoute |
| CNI | Cilium (flannel disabled) + Hubble | Pod networking, NetworkPolicy enforcement, flow visibility including drops |
| Network policy | default-deny per app namespace | Allow rules declared next to each app |
| Admission | Kyverno + Pod Security `restricted` | Allowed registries only, no `latest`, non-root, no privilege escalation |
| Audit | kube-apiserver audit policy | Secret reads/lists, `exec`, RBAC changes → log pipeline |
| Runtime security | Falco + Falcosidekick | Shell in container, unexpected file writes, etc. → alert-agent |
| Metrics | Prometheus + Alertmanager | Cluster, node, app (RED) and Hubble metrics |
| Logs | Loki via Grafana Alloy | Pod logs and audit log; survives pod replacement |
| Traces | Tempo via Alloy (OTLP) | Request traces, linked from metrics and logs |
| Dashboards / rules | Grafana | Alert rules, Viewer service account for the agent |
| Secrets | Sealed Secrets | Only encrypted secrets in Git; real values come from the local `.env` |

## 3. Sample workloads

Two fictional projects, each with a backend and a frontend, deployed to `develop` and `production`
namespaces. Every backend exposes fault-injection endpoints: 5xx on one route, crash loop, OOM, slow
responses, dependency down. A storm script reproduces "10 alerts in 2 minutes" and "80 identical 5xx".

## 4. Alert flow

```
fault in app ──► metrics/logs/traces ──► Alloy ──► Prometheus / Loki / Tempo
                                                     │
                  Grafana alert rules / Alertmanager ┘
                                  │ firing + resolved (webhook, in-cluster only)
Falco events ──► Falcosidekick ───┤
                                  ▼
                     alert-agent  intake
                                  │
                     dedupe by fingerprint (source, cluster, namespace/app, rule, endpoint)
                     correlate same project within a short window
                     60 s batching window for storms
                                  │
                     incident store (persistent, survives restarts)
                                  │
                     orchestrator: one sub-agent per incident, max N in parallel,
                     queue ordered by severity, daily cost cap
                                  │
             ┌────────────────────┼────────────────────┐
             ▼                    ▼                    ▼
     Kubernetes API         Grafana (Viewer)      OpenAI API
     pods/log, events       PromQL, LogQL         redacted snippets only
     (get/list/watch)
                                  │
                                  ▼
                     Telegram bot (long polling, outbound only)
                       • one diagnosis per incident
                       • close proposals → operator approves
                       • dev message draft (phase 1) / direct send (phase 2)
                       • 08:30 workday summary
```

### Alert rules shipped by the platform

| Rule | Source | Protected (never auto-closed) |
|---|---|---|
| 5xx rate per app/endpoint | Prometheus (RED metrics) | no |
| Pod restarting | kube-state-metrics | no |
| Deployment replicas missing | kube-state-metrics | no |
| Pod OOMKilled | kube-state-metrics | no |
| Node CPU/RAM high | node-exporter | no |
| Unexpected network drops | Hubble metrics | no |
| Runtime security event | Falco | yes |
| Sensitive audit action (Secret list/get outside known controllers, `exec`) | audit log in Loki | yes |
| Node not ready | kube-state-metrics | yes |
| PVC almost full / certificate expiring | kubelet / cert metrics | yes |

## 5. Agent permissions

| System | Identity | Allowed |
|---|---|---|
| Kubernetes | dedicated ServiceAccount | `get`, `list`, `watch` on `pods`, `pods/log`, `events`. No Secrets, no `exec`, no writes |
| Grafana | service account, role Viewer | query data sources, read dashboards |
| Telegram | bot token | post in the alert group; read commands only from allow-listed user IDs |
| OpenAI | API key | model calls with redacted input, daily spend cap |

Credentials are delivered as Sealed Secrets generated from the local `.env`; none are stored in Git in clear.
