"""Alerts to incidents: fingerprint key, folding of repeats, correlation, resolve handling (FR-002..006, FR-026, FR-027)."""
from datetime import timedelta

from .config import Settings
from .store import Store, now_iso, parse_iso

SEVERITY_RANK = {"info": 0, "warning": 1, "high": 2, "critical": 3}
SOURCES = {"loki": "loki-app"}
PROTECTED_SOURCES = ("loki-audit", "falco")


def normalise_severity(value: str | None) -> str:
    return value if value in SEVERITY_RANK else "warning"


def incident_fields(labels: dict) -> dict:
    """Incident columns and key from alert labels (audit adds the actor, runtime adds pod and node)."""
    source = SOURCES.get(labels.get("source", ""), labels.get("source") or "prometheus")
    namespace = labels.get("namespace") or labels.get("k8s_ns_name", "")
    service = labels.get("service") or labels.get("service_name", "")
    route = labels.get("http_route") or labels.get("route", "")
    rule = labels.get("alertname", "")
    if source == "loki-audit":
        parts = [rule, namespace, labels.get("name", ""), labels.get("user", "")]
    elif source == "falco":
        parts = [rule, labels.get("rule", ""), namespace, labels.get("k8s_pod_name", ""), labels.get("hostname", "")]
    else:
        parts = [rule, labels.get("rule_ref", ""), namespace, service, route]
    return {
        "key": "|".join([source, *parts]),
        "source": source,
        "rule": rule,
        "rule_ref": labels.get("rule_ref", ""),
        "namespace": namespace,
        "service": service,
        "route": route,
        "severity": normalise_severity(labels.get("severity")),
        "protected": int(source in PROTECTED_SOURCES or labels.get("protected") == "true"),
    }


async def process(store: Store, settings: Settings) -> int:
    """Fold every stored, not yet processed alert into an incident; returns how many alerts were handled."""
    alerts = await store.unprocessed_alerts()
    for a in alerts:
        labels = a["labels"]
        if labels.get("severity") == "none":  # internal marker such as ServiceRollingOut: used by the gate only
            await store.mark_alert(a["id"], None)
            continue
        fields = incident_fields(labels)
        inc = await store.open_incident_by_key(fields["key"])
        if a["status"] == "resolved":
            if inc:
                await _resolve(store, inc)
            await store.mark_alert(a["id"], inc["id"] if inc else None)
        elif inc:
            await _repeat(store, inc, fields, a["received_at"])
            await store.mark_alert(a["id"], inc["id"])
        else:
            incident_id = await _open(store, settings, fields, a["received_at"])
            await store.mark_alert(a["id"], incident_id)
    return len(alerts)


async def _open(store: Store, settings: Settings, fields: dict, seen: str) -> int:
    state = "queued" if fields["protected"] else "new"  # protected incidents skip batching and the gate
    incident_id = await store.upsert_incident({**fields, "state": state, "occurrences": 1, "first_seen": seen, "last_seen": seen})
    correlation_id = incident_id
    extra: dict = {}
    if fields["namespace"]:
        since = now_iso(parse_iso(seen) - timedelta(seconds=settings.correlation_window_seconds))
        actor = fields["key"].rsplit("|", 1)[-1]
        if fields["protected"]:
            peer = await store.queued_protected_peer(fields["namespace"], since, fields["rule"], actor)
        else:
            peer = await store.correlatable(fields["namespace"], since)
        if peer and peer["id"] != incident_id:
            correlation_id = peer["correlation_id"] or peer["id"]
        else:  # the group already runs or ran: wait for its verdict, then fold in or get its own run
            late = await store.fold_peer(fields["namespace"], since, fields["protected"], fields["rule"], actor)
            if late and late["id"] != incident_id:
                correlation_id = late["correlation_id"] or late["id"]
                extra = {"state": "folded", "gate_reason": f"chờ nhóm #{correlation_id} xong để gộp"}
    await store.upsert_incident({"id": incident_id, "correlation_id": correlation_id, **extra})
    return incident_id


async def _repeat(store: Store, inc: dict, fields: dict, seen: str) -> None:
    """A repeat only counts; it speaks again just when severity rises on an incident already reported (FR-006)."""
    update = {"id": inc["id"], "occurrences": inc["occurrences"] + 1, "last_seen": seen}
    if SEVERITY_RANK[fields["severity"]] > SEVERITY_RANK.get(inc["severity"], 1):
        update["severity"] = fields["severity"]
        if inc["state"] == "diagnosed":
            update.update(state="queued", gate_reason="severity tăng")
    await store.upsert_incident(update)


async def _resolve(store: Store, inc: dict) -> None:
    state = inc["state"]
    if state in ("new", "held"):
        return  # the gate sees the resolved status and closes it as self_resolved (rule 3)
    if state in ("queued", "analysing", "diagnosed", "reported_only", "folded"):
        if inc["protected"] and state == "queued":
            return  # a protected incident is still diagnosed even if it already recovered
        await store.set_state(inc["id"], "resolved")
