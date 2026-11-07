"""Deterministic context pack: the cluster facts the model would otherwise discover one tool call at a time."""
import asyncio
import re
from datetime import datetime, timezone

from .redact import redact
from .tools.k8s import K8s
from .tools.telemetry import Telemetry

MAX_PACK = 9000  # about 2.5k tokens, hard cap on the whole pack
CAPS = {"pods": 2800, "previous logs": 2000, "rollout": 700, "events": 1500, "network policies": 2200, "audit": 1800}
EVENT_MINUTES, AUDIT_MINUTES = 30, 60
_HASH_SUFFIX = re.compile(r"-[a-z0-9]{6,10}-[a-z0-9]{5}$")  # ReplicaSet and pod hash of a Deployment pod


def workload_name(service: str) -> str:
    """Deployment name behind a service label that may be a pod name (PodOOMKilled)."""
    return _HASH_SUFFIX.sub("", service)


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 8] + "…(cut)"


def _hm(ts: str) -> str:
    return ts[11:19] + "Z" if len(ts) >= 19 else ts or "?"


def _res(d: dict) -> str:
    return f"cpu={d.get('cpu', '-')} mem={d.get('memory', '-')}"


def _container_line(c: dict) -> str:
    last = f" last={c['last_reason']} exit={c['last_exit']} at={_hm(c['last_finished'])}" if c["last_reason"] else " last=-"
    state = c["state"] + (f"({c['state_reason']})" if c["state_reason"] else "")
    return (f"  {c['name']} ready={c['ready']} restarts={c['restarts']} state={state}{last}"
            f" requests[{_res(c['requests'])}] limits[{_res(c['limits'])}] image={c['image'].rsplit('/', 1)[-1]}")


def _unhealthy(p: dict) -> bool:
    return p["phase"] != "Running" or any(not c["ready"] or c["restarts"] or c["last_reason"] for c in p["containers"])


def _targets(pods: list[dict], services: set[str]) -> list[dict]:
    """Pods of the incident's workload; the service label is a Deployment name or, for OOM alerts, a pod name."""
    bases = {workload_name(s) for s in services if s}
    return [p for p in pods if p["app"] in bases or any(p["name"] == s or p["name"].startswith(b + "-") for s in services for b in bases if s)]


def _pods_section(pods: list[dict], targets: list[dict]) -> str:
    names = {p["name"] for p in targets}
    shown = targets + [p for p in pods if p["name"] not in names and _unhealthy(p)][:4]
    lines = [f"{p['name']} phase={p['phase']}{' (incident workload)' if p['name'] in names else ''}\n" + "\n".join(_container_line(c) for c in p["containers"])
             for p in shown]
    rest = len(pods) - len(shown)
    if rest:
        lines.append(f"({rest} other pods healthy)")
    return "\n".join(lines) or "no pods"


def _events_section(events: list[dict], targets: list[dict], bases: set[str]) -> str:
    names = {p["name"] for p in targets}
    keep = [e for e in events if e["type"] == "Warning" or e["object"] in names or e["object"] in bases]
    return "\n".join(f"{_hm(e['ts'])} {e['type']} {e['reason']} {e['object']}: {e['message'][:160]} x{e['count']}" for e in keep[-15:]) or "none"


async def _guard(coro, what: str) -> str:
    """A failed read becomes a visible gap in the pack, never an exception."""
    try:
        return await coro
    except Exception as e:
        return f"(unavailable: {what}: {type(e).__name__})"


async def _namespace_pack(ns: str, services: set[str], k8s: K8s, telemetry: Telemetry, now: datetime) -> str:
    pods_r, events_r, netpol, audit = await asyncio.gather(
        _guard(k8s.pods_detail(ns), "pods"),
        _guard(k8s.events_recent(ns, EVENT_MINUTES, now), "events"),
        _guard(k8s.list_network_policies(ns), "network policies"),
        _guard(telemetry.audit_changes(ns, AUDIT_MINUTES, 15), "audit"),
    )
    sections: list[tuple[str, str]] = []
    pods = pods_r if isinstance(pods_r, list) else []
    targets = _targets(pods, services)
    sections.append(("pods", _pods_section(pods, targets) if pods else str(pods_r)))
    # Previous-container log tail of crashed or killed pods: the stack trace or the last request before the kill.
    crashed = [p["name"] for p in (targets or [p for p in pods if _unhealthy(p)]) if any(c["last_reason"] for c in p["containers"])][:2]
    prev = await asyncio.gather(*[_guard(k8s.get_pod_logs(ns, n, 30, True, None), f"previous logs {n}") for n in crashed])
    sections.append(("previous logs", "\n".join(f"[{n}]\n" + "\n".join(t.splitlines()[-12:]) for n, t in zip(crashed, prev) if not t.startswith("(unavailable")) or "none"))
    deployments = sorted({p["app"] for p in targets if p["app"]})[:2]
    rollouts = await asyncio.gather(*[_guard(k8s.get_rollout(ns, d), f"rollout {d}") for d in deployments])
    sections.append(("rollout", "\n".join(f"{d}: {r}" for d, r in zip(deployments, rollouts)) or "no deployment resolved"))
    sections.append(("events", _events_section(events_r, targets, set(deployments)) if isinstance(events_r, list) else events_r))
    sections.append(("network policies", netpol))
    audit_text = "no writes by people or pipelines in the last 60 min" if audit == "no log lines" else audit
    sections.append(("audit", audit_text))
    head = f"== namespace {ns} (UTC now {now.strftime('%H:%M:%S')}Z) =="
    return head + "\n" + "\n".join(f"[{name}]\n{_cap(body, CAPS[name])}" for name, body in sections)


async def build_pack(incidents: list[dict], k8s: K8s, telemetry: Telemetry, now: datetime | None = None) -> str:
    """Compact, redacted facts for the incident group's namespaces; empty when no namespace is readable. Never raises."""
    now = now or datetime.now(timezone.utc)
    by_ns: dict[str, set[str]] = {}
    for inc in incidents:
        if inc["namespace"] and k8s.allowed(inc["namespace"]):
            by_ns.setdefault(inc["namespace"], set()).add(inc["service"] or "")
    if not by_ns:
        return ""
    try:
        packs = await asyncio.gather(*[_namespace_pack(ns, svc, k8s, telemetry, now) for ns, svc in by_ns.items()])
    except Exception:
        return ""
    return _cap(redact("\n".join(packs)), MAX_PACK)
