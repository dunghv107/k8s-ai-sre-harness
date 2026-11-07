from datetime import datetime, timedelta, timezone

import pytest

from alert_agent import gate
from alert_agent.app import _parse
from alert_agent.config import Settings
from alert_agent.orchestrator import Orchestrator
from alert_agent.store import Store, now_iso
from conftest import load_fixture, make_alert, make_settings

NOW = datetime.now(timezone.utc)


class StubK8s:
    list_pods = get_pod_logs = get_events = get_rollout = list_network_policies = None  # only the Toolbox constructor looks at these
    def __init__(self, rolling=False, pods=None, allowed=True):
        self._rolling, self._pods, self._allowed = rolling, pods if pods is not None else [], allowed

    def allowed(self, ns):
        return self._allowed

    async def rollout(self, ns, dep):
        return {"rolling": self._rolling, "revision": "7"}

    async def pods(self, ns, selector, now):
        return self._pods


class StubTelemetry:
    query_metrics = query_logs = get_trace = search_traces = None
    def __init__(self, errors=10):
        self.errors = errors

    async def count_errors(self, ns, svc, seconds):
        return self.errors


def pod(age, ready=False, restarts=1, phase="Running"):
    return {"name": "catalog-api-abc-xyz", "phase": phase, "ready": ready, "restarts": restarts, "age_s": age, "last_reason": ""}


def incident(**over):
    base = {"id": 1, "protected": 0, "namespace": "shop", "service": "catalog-api", "rule": "Http5xxRate"}
    return {**base, **over}


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "gate.db"))
    yield s
    s.close()


async def decide(store, inc=None, k8s=None, tel=None, **settings):
    return await gate.evaluate(inc or incident(), settings=Settings(**settings), store=store, k8s=k8s or StubK8s(),
                               telemetry=tel or StubTelemetry(), now=NOW)


async def link_alert(store, status):
    """Create the incident row and one alert for it with the given status."""
    incident_id = await store.upsert_incident({"key": "k", "state": "held", "first_seen": now_iso(), "last_seen": now_iso()})
    await store.insert_alerts(_parse({"alerts": [make_alert(status=status)]}))
    (alert,) = await store.unprocessed_alerts()
    await store.mark_alert(alert["id"], incident_id)
    return incident_id


async def test_rule1_marker_holds(store):
    await store.insert_alerts(_parse(load_fixture("am_rollout_marker.json")))
    d = await decide(store)
    assert (d.action, d.kind) == ("hold", "rollout") and "catalog-api" in d.reason


async def test_rule1_marker_resolved_does_not_hold(store):
    marker = load_fixture("am_rollout_marker.json")
    await store.insert_alerts(_parse(marker))
    marker["alerts"][0]["status"] = "resolved"
    await store.insert_alerts(_parse(marker))
    assert (await decide(store)).action == "queue"


async def test_rule1_generation_lag_holds(store):
    d = await decide(store, k8s=StubK8s(rolling=True))
    assert (d.action, d.kind) == ("hold", "rollout") and "rev 7" in d.reason


async def test_rule2_young_failing_pods_hold(store):
    d = await decide(store, k8s=StubK8s(pods=[pod(40)]))
    assert (d.action, d.kind) == ("hold", "young_pod") and "40 giây" in d.reason


async def test_rule2_one_old_failing_pod_lets_it_through(store):
    d = await decide(store, k8s=StubK8s(pods=[pod(40), pod(900)]))
    assert d.action == "queue"


async def test_rule2_healthy_young_pods_are_not_failing(store):
    d = await decide(store, k8s=StubK8s(pods=[pod(40, ready=True, restarts=0)]))
    assert d.action == "queue"


async def test_rule3_resolved_during_hold_is_self_resolved(store):
    incident_id = await link_alert(store, "resolved")
    d = await decide(store, inc=incident(id=incident_id))
    assert d.action == "self_resolved"


async def test_rule4_below_threshold_holds(store):
    d = await decide(store, tel=StubTelemetry(errors=2))
    assert (d.action, d.kind) == ("hold", "below_threshold")
    assert (await decide(store, tel=StubTelemetry(errors=3))).action == "queue"


async def test_rule4_only_for_error_rate_rules(store):
    d = await decide(store, inc=incident(rule="PodRestartingFrequently"), tel=StubTelemetry(errors=0))
    assert d.action == "queue"


async def test_rule4_unknown_count_does_not_block(store):
    assert (await decide(store, tel=StubTelemetry(errors=None))).action == "queue"


async def test_protected_bypasses_everything(store):
    await store.insert_alerts(_parse(load_fixture("am_rollout_marker.json")))
    d = await decide(store, inc=incident(protected=1), k8s=StubK8s(rolling=True, pods=[pod(5)]), tel=StubTelemetry(errors=0))
    assert d.action == "queue"


async def test_namespace_without_access_skips_cluster_checks(store):
    assert (await decide(store, k8s=StubK8s(rolling=True, allowed=False))).action == "queue"


async def test_ten_minute_ceiling(tmp_path, fakes):
    """Held for a rollout that never ends: after 10 minutes the incident is queued anyway."""
    s = make_settings(tmp_path, max_parallel_subagents=0)  # nothing is dispatched: only the gate is under test
    store = Store(s.db_path)
    k8s, tel = StubK8s(rolling=True), StubTelemetry()
    orch = Orchestrator(s, store, k8s, tel, None)
    await store.insert_alerts(_parse({"alerts": [make_alert(service="catalog-api", namespace="shop")]}))
    t0 = datetime.now(timezone.utc)
    await orch.tick(t0)
    (inc,) = await store.incidents_in_state("held")
    await orch.tick(t0 + timedelta(minutes=9, seconds=50))
    assert (await store.get_incident(inc["id"]))["state"] == "held"
    await orch.tick(t0 + timedelta(minutes=10, seconds=5))
    done = await store.get_incident(inc["id"])
    assert done["state"] in ("queued", "analysing") and "hết hạn hoãn" in done["gate_reason"]
    store.close()


async def test_ceiling_below_threshold_is_reported_only(tmp_path, fakes):
    s = make_settings(tmp_path, max_parallel_subagents=0)  # nothing is dispatched: only the gate is under test
    store = Store(s.db_path)
    orch = Orchestrator(s, store, StubK8s(), StubTelemetry(errors=1), None)
    await store.insert_alerts(_parse({"alerts": [make_alert(service="catalog-api", namespace="shop")]}))
    t0 = datetime.now(timezone.utc)
    await orch.tick(t0)
    await orch.tick(t0 + timedelta(minutes=11))
    (inc,) = await store.incidents_in_state("reported_only")
    assert "ghi nhận" in inc["gate_reason"]
    store.close()


class RecordingK8s(StubK8s):
    def __init__(self, error=None, **kw):
        super().__init__(**kw)
        self.error, self.asked = error, []

    async def rollout(self, ns, dep):
        self.asked.append(dep)
        if self.error:
            raise self.error
        return await super().rollout(ns, dep)


async def test_pod_name_service_label_resolves_to_its_deployment(store):
    k8s = RecordingK8s(rolling=True)
    d = await decide(store, inc=incident(service="catalog-api-54bb788c96-hpmjk", rule="PodOOMKilled"), k8s=k8s)
    assert k8s.asked == ["catalog-api"] and d.action == "hold" and "catalog-api" in d.reason


async def test_non_workload_label_gets_a_clear_gate_reason(store):
    from alert_agent.tools.k8s import ToolError

    k8s = RecordingK8s(error=ToolError("kubernetes API returned 404 for /apis/apps/v1/namespaces/shop/deployments/network"))
    d = await decide(store, inc=incident(service="network", rule="HubblePolicyDrops"), k8s=k8s)
    assert d.action == "queue" and "'network' không phải Deployment" in d.reason


async def test_unreachable_cluster_is_recorded_in_the_gate_reason(store):
    d = await decide(store, k8s=RecordingK8s(error=RuntimeError("boom")))
    assert d.action == "queue" and "không đọc được trạng thái cụm (RuntimeError)" in d.reason
