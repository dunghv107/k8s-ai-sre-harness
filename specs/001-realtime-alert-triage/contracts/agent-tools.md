# Contract: tools exposed to the sub-agent

All tools are read-only, run inside the agent process, return at most 6 000 characters after redaction, and
log their call to the run record. Namespaces are restricted to an allow-list (`shop` by default).

| Tool | Arguments | Backend call |
|---|---|---|
| `list_pods` | `namespace`, `selector` | `GET /api/v1/namespaces/{ns}/pods?labelSelector=` → name, phase, ready, restarts, age, last termination reason |
| `get_pod_logs` | `namespace`, `pod`, `since_minutes` (≤60), `previous` (bool), `grep` (optional) | `GET .../pods/{pod}/log?sinceSeconds=&tailLines=400&previous=` then filter |
| `get_events` | `namespace`, `object_name` | `GET /api/v1/namespaces/{ns}/events?fieldSelector=involvedObject.name=` |
| `get_rollout` | `namespace`, `deployment` | `GET /apis/apps/v1/.../deployments/{name}` + replicasets → generation, updated/ready replicas, image, revision age |
| `list_network_policies` | `namespace` | `GET /apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies` → name, selected pods, allowed ingress/egress peers and ports, last modification time and manager (from `managedFields`) |
| `query_metrics` | `promql`, `minutes` (≤120) | Prometheus `query_range`, step chosen to return ≤ 60 points per series, ≤ 10 series |
| `query_logs` | `logql`, `minutes` (≤120), `limit` (≤50) | Loki `query_range`, newest first |
| `get_trace` | `trace_id` | Tempo `/api/traces/{id}` → span tree summary: service, name, duration, status, http.route, error message |
| `search_traces` | `service`, `minutes`, `status_error` (bool) | Tempo `/api/search` with TraceQL `{resource.service.name="x" && status=error}` → up to 5 trace ids |

Before the model starts, the agent also builds a deterministic context pack (`pack.py`, at most 9 000 characters, redacted) from the same read-only sources: pod and container state with last termination and resources, events of the last 30 minutes, rollout, NetworkPolicies, writes of the last 60 minutes from the Kubernetes audit stream (Loki `{job="k8s-audit"}`), and the previous-container log tail of crashed pods. It is placed in the first user message; failed reads show as `(unavailable: ...)`.

Not available, by design: Secrets, exec, port-forward, any write verb, cluster-scoped listing.
