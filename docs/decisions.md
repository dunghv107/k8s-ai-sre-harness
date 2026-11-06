# Design decisions

Short records of the choices behind the homelab and the alert agent. Each one states the context, the
decision and what was given up.

## D-01 · Local k3d instead of AKS

- **Context:** the homelab must behave like a production cluster (RBAC, NetworkPolicy, audit log, CNI choice)
  and stay free to run.
- **Decision:** k3s in Docker via k3d, 1 server + 2 agents, on the workstation.
- **Trade-off:** no managed control plane, no real cloud load balancer, host must stay awake at night.
  AKS (~2 × B2s) is the fallback if local resources are not enough.

## D-02 · Cilium + Hubble instead of the default flannel

- **Context:** the agent must handle network alerts (dropped flows), and policies must be enforced.
- **Decision:** disable flannel and k3s' built-in network policy controller; install Cilium with Hubble.
- **Trade-off:** more moving parts at bootstrap; one tool owns both enforcement and visibility, which avoids
  splitting the job between two CNIs.

## D-03 · Falco instead of Wazuh

- **Context:** runtime security alerts are needed; Wazuh (indexer + manager + dashboard) needs 6–8 GB RAM.
- **Decision:** Falco + Falcosidekick, events pushed straight to the agent.
- **Trade-off:** no host-level SIEM, no log retention inside the security tool (Loki keeps logs).

## D-04 · Real-time processing instead of a nightly batch

- **Context:** the first idea was to analyse at 06:00 and report at 08:15. Diagnoses would be stale and a
  storm at 23:00 would be analysed hours later.
- **Decision:** every alert is processed as it arrives; storms are absorbed by a 60 s batching window and
  fingerprint grouping; one sub-agent per incident, bounded parallelism. The 08:30 report is only a view.
- **Trade-off:** the model is called around the clock, so a daily cost cap is required.

## D-05 · Sub-agent per incident, not per alert

- **Context:** 80 identical 5xx alerts must not cost 80 model runs or produce 80 messages.
- **Decision:** dedupe by fingerprint and correlate by project/time before any model call.
- **Trade-off:** a bad fingerprint can merge two different problems; the incident keeps every source alert ID
  so the operator can split it.

## D-06 · OpenAI API as the model provider

- **Context:** the operator holds an OpenAI API key.
- **Decision:** sub-agents call the OpenAI API. Only redacted log snippets needed for the diagnosis are sent,
  never whole log files.
- **Trade-off:** data leaves the homelab (redacted). A local model would avoid that but needs GPU/RAM the
  host does not have.

## D-07 · Telegram with long polling

- **Context:** the operator wants alerts and commands on the phone; the homelab has no public endpoint.
- **Decision:** a Telegram bot using long polling (outbound HTTPS only). Commands accepted only from
  allow-listed user IDs.
- **Trade-off:** a few seconds of command latency compared with webhooks; no port opened to the Internet.

## D-08 · Human approval first, automation earned

- **Context:** a wrong auto-close or a wrong message to developers costs trust.
- **Decision:** phase 1 = proposals and drafts only. Auto-close only for patterns the operator marked
  trusted. Direct messages to developers are enabled per project after ≥ 4 weeks and ≥ 20 drafts with
  ≥ 90 % sent unchanged (spec 001, SC-009).
- **Trade-off:** slower to reach full automation.

## D-10 · Deterministic gate before any model call

- **Context:** many alerts are transient: a new pod returns 5xx while it starts, a rollout replaces pods, one
  restart then stability. Sending those to a model costs money and adds noise.
- **Decision:** two layers. Alertmanager inhibits 5xx / restart / replicas-missing alerts of a service while that
  service is rolling out. The harness then holds remaining non-protected alerts for a grace period and checks pod age,
  rollout state and error count through the read-only API; only what is still failing reaches a sub-agent.
  Protected alerts (security, audit, runtime, node, storage, certificates) skip the gate.
- **Trade-off:** a real bug introduced by a release is diagnosed a few minutes later (rollout + grace), never missed.

## D-09 · Spec-Driven Development with GitHub Spec Kit

- **Context:** the project is built mostly by a coding agent and published as a portfolio piece.
- **Decision:** constitution → spec → plan → tasks → implement, all artifacts versioned in `specs/`.
- **Trade-off:** more writing up front; in exchange every requirement is traceable to a test.
