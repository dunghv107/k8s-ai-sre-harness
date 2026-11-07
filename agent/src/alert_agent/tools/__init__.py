"""Tool schemas (contracts/agent-tools.md) and dispatch. Every tool is read-only."""
from .k8s import K8s, ToolError
from .telemetry import Telemetry


def _fn(name: str, description: str, props: dict, nullable: tuple[str, ...] = ()) -> dict:
    """Strict function tool: every property required, optional ones nullable."""
    properties = {
        k: ({"type": [v["type"], "null"]} if k in nullable else dict(v)) | {"description": v["description"]}
        for k, v in props.items()
    }
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {"type": "object", "properties": properties, "required": list(props), "additionalProperties": False},
    }


def _s(description: str) -> dict:
    return {"type": "string", "description": description}


def _i(description: str) -> dict:
    return {"type": "integer", "description": description}


def _b(description: str) -> dict:
    return {"type": "boolean", "description": description}


TOOL_SPECS = [
    _fn("list_pods", "List pods of a namespace by label selector: phase, ready, restarts, age, last termination reason.",
        {"namespace": _s("Namespace"), "selector": _s("Label selector, e.g. app.kubernetes.io/name=catalog-api")}),
    _fn("get_pod_logs", "Read recent log lines of one pod (newest 400 lines max, redacted).",
        {"namespace": _s("Namespace"), "pod": _s("Pod name"), "since_minutes": _i("Look back this many minutes, at most 60"),
         "previous": _b("true for the previous (crashed) container"), "grep": _s("Keep only lines containing this text (case-insensitive), or null")},
        nullable=("grep",)),
    _fn("get_events", "Kubernetes events of one object (pod, deployment, replicaset).",
        {"namespace": _s("Namespace"), "object_name": _s("Name of the involved object")}),
    _fn("get_rollout", "Deployment rollout state: generation, updated and ready replicas, image, revisions.",
        {"namespace": _s("Namespace"), "deployment": _s("Deployment name")}),
    _fn("list_network_policies", "NetworkPolicies of a namespace: selected pods, allowed ingress/egress peers and ports, last modification time and manager.",
        {"namespace": _s("Namespace")}),
    _fn("query_metrics", "Prometheus range query (at most 60 points per series, 10 series).",
        {"promql": _s("PromQL expression"), "minutes": _i("Look back this many minutes, at most 120")}),
    _fn("query_logs", "Loki range query, newest lines first.",
        {"logql": _s("LogQL expression"), "minutes": _i("Look back this many minutes, at most 120"), "limit": _i("Max lines, at most 50")}),
    _fn("get_trace", "Tempo trace summary: services, spans, durations, status, route, error message.",
        {"trace_id": _s("Hex trace id")}),
    _fn("search_traces", "Find up to 5 trace ids of a service in Tempo.",
        {"service": _s("service.name"), "minutes": _i("Look back this many minutes"), "status_error": _b("true for error traces only")}),
]


class Toolbox:
    def __init__(self, k8s: K8s, telemetry: Telemetry):
        self._tools = {
            "list_pods": k8s.list_pods,
            "get_pod_logs": k8s.get_pod_logs,
            "get_events": k8s.get_events,
            "get_rollout": k8s.get_rollout,
            "list_network_policies": k8s.list_network_policies,
            "query_metrics": telemetry.query_metrics,
            "query_logs": telemetry.query_logs,
            "get_trace": telemetry.get_trace,
            "search_traces": telemetry.search_traces,
        }

    async def call(self, name: str, args: dict) -> str:
        """Run a tool; failures become text for the model instead of exceptions."""
        fn = self._tools.get(name)
        if fn is None:
            return f"error: unknown tool {name}"
        try:
            return await fn(**args)
        except ToolError as e:
            return f"error: {e}"
        except Exception as e:  # network errors, bad arguments: the model may retry differently
            return f"error: {type(e).__name__}: {str(e)[:200]}"
