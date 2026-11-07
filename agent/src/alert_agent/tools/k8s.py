"""Read-only Kubernetes access: plain GET requests, no client library."""
import os
import re
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..redact import redact

MAX_OUTPUT = 6000
_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


class ToolError(Exception):
    pass


def clip(text: str, keep: str = "head") -> str:
    """Redact, then cut to the output limit; logs keep their tail because the newest lines matter."""
    text = redact(text)
    if len(text) <= MAX_OUTPUT:
        return text
    return "…(truncated)\n" + text[-MAX_OUTPUT:] if keep == "tail" else text[:MAX_OUTPUT] + "\n…(truncated)"


def _age(ts: str | None, now: datetime) -> int:
    return int((now - datetime.fromisoformat(ts)).total_seconds()) if ts else 0


def _selector(sel: dict | None) -> str:
    if not sel:
        return "all"
    parts = [f"{k}={v}" for k, v in (sel.get("matchLabels") or {}).items()]
    parts += [f"{e.get('key')} {e.get('operator')} ({','.join(e.get('values') or [])})" for e in sel.get("matchExpressions") or []]
    return "{" + "; ".join(parts) + "}"


def _peers(rules: list[dict], key: str) -> str:
    """Allowed peers and ports per rule; an empty rule list means everything is denied."""
    if not rules:
        return "deny all"
    out = []
    for r in rules:
        peers = []
        for p in r.get(key) or []:
            if "ipBlock" in p:
                peers.append("cidr " + p["ipBlock"].get("cidr", "?"))
            else:
                bits = ["ns" + _selector(p["namespaceSelector"])] if "namespaceSelector" in p else []
                bits += ["pod" + _selector(p["podSelector"])] if "podSelector" in p else []
                peers.append(" ".join(bits) or "any")
        ports = ",".join(f"{p.get('port', '*')}/{p.get('protocol', 'TCP')}" for p in r.get("ports") or [])
        out.append(f"{' | '.join(peers) or 'any peer'} ports {ports or 'all'}")
    return " ; ".join(out)


def _policy_summary(spec: dict) -> str:
    types = spec.get("policyTypes") or ["Ingress"]
    parts = [f"selects pods {_selector(spec.get('podSelector'))}"]
    if "Ingress" in types:
        parts.append("ingress from: " + _peers(spec.get("ingress") or [], "from"))
    if "Egress" in types:
        parts.append("egress to: " + _peers(spec.get("egress") or [], "to"))
    return "; ".join(parts)


class K8s:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        verify = settings.k8s_ca_path if os.path.exists(settings.k8s_ca_path) else True
        self._client = client or httpx.AsyncClient(base_url=settings.k8s_url, verify=verify, timeout=15)

    def allowed(self, namespace: str) -> bool:
        return namespace in self.s.namespaces

    def _ns(self, namespace: str) -> str:
        if not self.allowed(namespace):
            raise ToolError(f"namespace '{namespace}' is not allowed (allowed: {', '.join(self.s.namespaces)})")
        return namespace

    @staticmethod
    def _name(value: str, what: str) -> str:
        if not _NAME.match(value):
            raise ToolError(f"invalid {what}")
        return value

    async def _get(self, path: str, **params) -> httpx.Response:
        """The only HTTP call in this module, and it is a GET."""
        headers = {}
        if os.path.exists(self.s.k8s_token_path):  # re-read each time: projected tokens rotate
            with open(self.s.k8s_token_path) as f:
                headers["Authorization"] = "Bearer " + f.read().strip()
        r = await self._client.get(path, params={k: v for k, v in params.items() if v is not None}, headers=headers)
        if r.status_code >= 400:
            raise ToolError(f"kubernetes API returned {r.status_code} for {path}")
        return r

    # --- structured reads (also used by the gate) ---
    async def pods(self, namespace: str, selector: str, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        data = (await self._get(f"/api/v1/namespaces/{self._ns(namespace)}/pods", labelSelector=selector)).json()
        out = []
        for p in data.get("items", []):
            statuses = p.get("status", {}).get("containerStatuses", [])
            reason = ""
            for cs in statuses:
                terminated = cs.get("lastState", {}).get("terminated") or {}
                waiting = cs.get("state", {}).get("waiting") or {}
                reason = terminated.get("reason") or waiting.get("reason") or reason
            out.append(
                {
                    "name": p["metadata"]["name"],
                    "phase": p.get("status", {}).get("phase", ""),
                    "ready": bool(statuses) and all(cs.get("ready") for cs in statuses),
                    "restarts": sum(cs.get("restartCount", 0) for cs in statuses),
                    "age_s": _age(p["metadata"].get("creationTimestamp"), now),
                    "last_reason": reason,
                }
            )
        return out

    async def rollout(self, namespace: str, deployment: str) -> dict:
        ns, dep = self._ns(namespace), self._name(deployment, "deployment")
        d = (await self._get(f"/apis/apps/v1/namespaces/{ns}/deployments/{dep}")).json()
        spec, status = d.get("spec", {}), d.get("status", {})
        replicas = spec.get("replicas", 1)
        info = {
            "generation": d["metadata"].get("generation", 0),
            "observed_generation": status.get("observedGeneration", 0),
            "replicas": replicas,
            "updated": status.get("updatedReplicas", 0),
            "ready": status.get("readyReplicas", 0),
            "image": ",".join(c.get("image", "") for c in spec.get("template", {}).get("spec", {}).get("containers", [])),
            "revision": d["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "?"),
            "replicasets": [],
        }
        info["rolling"] = info["observed_generation"] != info["generation"] or info["updated"] < replicas
        match = spec.get("selector", {}).get("matchLabels", {})
        if match:
            selector = ",".join(f"{k}={v}" for k, v in match.items())
            rs = (await self._get(f"/apis/apps/v1/namespaces/{ns}/replicasets", labelSelector=selector)).json()
            now = datetime.now(timezone.utc)
            for r in rs.get("items", []):
                info["replicasets"].append(
                    {
                        "name": r["metadata"]["name"],
                        "revision": r["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "?"),
                        "replicas": r.get("status", {}).get("replicas", 0),
                        "ready": r.get("status", {}).get("readyReplicas", 0),
                        "age_s": _age(r["metadata"].get("creationTimestamp"), now),
                    }
                )
        return info

    # --- structured reads for the context pack ---
    async def pods_detail(self, namespace: str) -> list[dict]:
        """Every pod of the namespace with per-container state, last termination and resources."""
        data = (await self._get(f"/api/v1/namespaces/{self._ns(namespace)}/pods")).json()
        out = []
        for p in data.get("items", []):
            specs = {c.get("name"): c for c in p.get("spec", {}).get("containers", [])}
            containers = []
            for cs in p.get("status", {}).get("containerStatuses", []):
                state = cs.get("state") or {}
                kind = next(iter(state), "unknown")
                now_s = state.get(kind) or {}
                last = (cs.get("lastState") or {}).get("terminated") or {}
                res = specs.get(cs.get("name"), {}).get("resources", {})
                containers.append(
                    {
                        "name": cs.get("name", ""), "ready": bool(cs.get("ready")), "restarts": cs.get("restartCount", 0),
                        "state": kind, "state_reason": now_s.get("reason", ""),
                        "last_reason": last.get("reason", ""), "last_exit": last.get("exitCode"), "last_finished": last.get("finishedAt", ""),
                        "requests": res.get("requests", {}), "limits": res.get("limits", {}), "image": cs.get("image", ""),
                    }
                )
            meta = p["metadata"]
            out.append(
                {
                    "name": meta["name"], "app": (meta.get("labels") or {}).get("app.kubernetes.io/name", ""),
                    "phase": p.get("status", {}).get("phase", ""), "containers": containers,
                }
            )
        return out

    async def events_recent(self, namespace: str, minutes: int = 30, now: datetime | None = None) -> list[dict]:
        """Namespace events newer than `minutes`, oldest first."""
        now = now or datetime.now(timezone.utc)
        items = (await self._get(f"/api/v1/namespaces/{self._ns(namespace)}/events")).json().get("items", [])
        out = []
        for e in items:
            ts = e.get("lastTimestamp") or e.get("eventTime") or ""
            try:
                if (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() > minutes * 60:
                    continue
            except ValueError:
                continue
            out.append({"ts": ts, "type": e.get("type", ""), "reason": e.get("reason", ""), "count": e.get("count", 1),
                        "object": (e.get("involvedObject") or {}).get("name", ""), "message": e.get("message", "")})
        return sorted(out, key=lambda e: e["ts"])

    async def network_policies(self, namespace: str) -> list[dict]:
        data = (await self._get(f"/apis/networking.k8s.io/v1/namespaces/{self._ns(namespace)}/networkpolicies")).json()
        out = []
        for n in data.get("items", []):
            meta = n.get("metadata", {})
            fields = sorted(meta.get("managedFields") or [], key=lambda m: m.get("time", ""))
            out.append(
                {"name": meta.get("name", ""), "created": meta.get("creationTimestamp", ""),
                 "modified": fields[-1].get("time", "") if fields else "", "last_manager": fields[-1].get("manager", "") if fields else "",
                 "summary": _policy_summary(n.get("spec", {}))}
            )
        return out

    # --- tools: text for the model ---
    async def list_pods(self, namespace: str, selector: str) -> str:
        rows = await self.pods(namespace, selector)
        if not rows:
            return "no pods match"
        return clip(
            "\n".join(
                f"{p['name']} phase={p['phase']} ready={p['ready']} restarts={p['restarts']} age={p['age_s']}s"
                f" last_reason={p['last_reason'] or '-'}"
                for p in rows
            )
        )

    async def get_pod_logs(self, namespace: str, pod: str, since_minutes: int = 15, previous: bool = False, grep: str | None = None) -> str:
        ns, name = self._ns(namespace), self._name(pod, "pod")
        since = max(1, min(int(since_minutes), 60)) * 60
        r = await self._get(
            f"/api/v1/namespaces/{ns}/pods/{name}/log",
            sinceSeconds=since,
            tailLines=400,
            previous="true" if previous else None,
        )
        lines = r.text.splitlines()
        if grep:
            lines = [ln for ln in lines if grep.lower() in ln.lower()]  # plain substring: no regex from the model
        return clip("\n".join(lines) or "no log lines", keep="tail")

    async def get_events(self, namespace: str, object_name: str) -> str:
        ns = self._ns(namespace)
        r = await self._get(f"/api/v1/namespaces/{ns}/events", fieldSelector=f"involvedObject.name={self._name(object_name, 'object_name')}")
        items = sorted(r.json().get("items", []), key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        if not items:
            return "no events"
        return clip(
            "\n".join(
                f"{e.get('lastTimestamp') or e.get('eventTime') or '?'} {e.get('type', '')} {e.get('reason', '')}"
                f" x{e.get('count', 1)}: {e.get('message', '')}"
                for e in items[-30:]
            )
        )

    async def get_rollout(self, namespace: str, deployment: str) -> str:
        i = await self.rollout(namespace, deployment)
        lines = [
            f"generation={i['generation']} observed={i['observed_generation']} replicas={i['replicas']}"
            f" updated={i['updated']} ready={i['ready']} rolling={i['rolling']}",
            f"image={i['image']} revision={i['revision']}",
        ]
        lines += [f"replicaset {r['name']} rev={r['revision']} replicas={r['replicas']} ready={r['ready']} age={r['age_s']}s" for r in i["replicasets"]]
        return clip("\n".join(lines))

    async def list_network_policies(self, namespace: str) -> str:
        rows = await self.network_policies(namespace)
        if not rows:
            return "no network policies"
        return clip("\n".join(f"{r['name']} created={r['created']} last_modified={r['modified'] or '?'} by={r['last_manager'] or '?'}\n  {r['summary']}" for r in rows))

    async def aclose(self) -> None:
        await self._client.aclose()
