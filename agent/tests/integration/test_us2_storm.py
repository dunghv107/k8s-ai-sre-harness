from alert_agent.store import Store
from conftest import make_alert, make_settings


def a(name="Http5xxRate", ns="shop", svc="catalog-api", route="/r", **kw):
    return make_alert(alertname=name, namespace=ns, service=svc, http_route=route, **kw)


async def test_ten_alerts_four_incidents_four_parallel_runs(make_harness, fakes):
    """6 repeats of one key, 2 of a second, and two distinct ones in other projects."""
    h = make_harness(namespaces=("shop", "team-a", "team-b", "team-c"))
    fakes.openai.delay = 0.05  # keeps the runs overlapping so concurrency is observable
    storm = [a() for _ in range(6)] + [a(ns="team-a", svc="svc-a")] * 2 + [a(ns="team-b", svc="svc-b"), a(ns="team-c", svc="svc-c")]
    assert len(storm) == 10
    await h.post(*storm)
    await h.settle()

    incidents = await h.incidents()
    assert len(incidents) == 4 and sorted(i["occurrences"] for i in incidents) == [1, 1, 2, 6]
    assert all(i["state"] == "diagnosed" for i in incidents)
    assert len(fakes.telegram_bodies) == 4 and len({b["text"] for b in fakes.telegram_bodies}) == 4
    assert len(await h.runs()) == 4
    assert 2 <= fakes.openai.max_active <= 5  # parallel, and never above the cap

    # Repeats of an open incident only count.
    await h.post(*[a() for _ in range(3)])
    await h.settle()
    assert len(fakes.telegram_bodies) == 4 and len(await h.runs()) == 4
    assert sorted(i["occurrences"] for i in await h.incidents()) == [1, 1, 2, 9]


async def test_concurrency_never_exceeds_the_cap(make_harness, fakes):
    h = make_harness(max_parallel_subagents=2, namespaces=tuple(f"team-{i}" for i in range(6)))
    fakes.openai.delay = 0.05
    await h.post(*[a(ns=f"team-{i}", svc=f"svc-{i}") for i in range(6)])
    await h.settle()
    assert fakes.openai.max_active == 2 and len(fakes.telegram_bodies) == 6


async def test_queue_is_ordered_by_severity(make_harness, fakes):
    h = make_harness(max_parallel_subagents=1, namespaces=("team-a", "team-b"))
    await h.post(a(name="WarnRule", ns="team-a", svc="s1", severity="warning"), a(name="CritRule", ns="team-b", svc="s2", severity="critical"))
    await h.settle()
    assert "CritRule" in fakes.telegram_bodies[0]["text"] and "WarnRule" in fakes.telegram_bodies[1]["text"]


async def test_same_project_incidents_share_one_run(make_harness, fakes):
    h = make_harness()
    await h.post(a(svc="catalog-api"), a(name="PodRestartingFrequently", svc="catalog-api", severity="warning", route=""), a(svc="orders-api"))
    await h.settle()
    incidents = await h.incidents()
    assert len(incidents) == 3 and len({i["correlation_id"] for i in incidents}) == 1
    assert len(await h.runs()) == 1 and len(fakes.telegram_bodies) == 1
    text = fakes.telegram_bodies[0]["text"]
    assert "#1 #2 #3" in text and "orders-api" in text
    assert len({i["message_id"] for i in incidents}) == 1


async def test_protected_incident_is_never_correlated(make_harness, fakes):
    h = make_harness()
    await h.post(a(), make_alert(alertname="AuditSecretsListByUser", source="loki-audit", severity="high", protected="true", namespace="shop", user="u", name="s"))
    await h.settle()
    assert len(await h.runs()) == 2 and len(fakes.telegram_bodies) == 2


async def test_severity_rise_on_a_reported_incident_reports_again(make_harness, fakes):
    h = make_harness()
    await h.post(a(severity="warning"))
    await h.settle()
    await h.post(a(severity="warning"))
    await h.settle()
    assert len(fakes.telegram_bodies) == 1  # same severity: silent
    await h.post(a(severity="critical"))
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["severity"] == "critical" and inc["occurrences"] == 3 and len(fakes.telegram_bodies) == 2


async def test_alerts_posted_while_down_are_not_lost(make_harness, fakes, tmp_path):
    """SC-010: stored alerts survive a restart; interrupted analyses are re-queued."""
    h1 = make_harness(namespaces=("shop", "team-a", "team-b"))
    await h1.post(*[a() for _ in range(4)], a(ns="team-a", svc="svc-a"))
    await h1.orch.tick()  # intake only: batching window is 0, gate and dispatch also run, so stop the task right away
    for task in list(h1.orch.running):
        task.cancel()
    await h1.orch.drain()
    h1.store.close()

    h2 = make_harness(namespaces=("shop", "team-a", "team-b"))  # same database file: a new process
    h2.store = Store(h2.s.db_path)
    h2.orch.store = h2.store
    await h2.orch.recover()
    await h2.post(a(ns="team-b", svc="svc-b"))  # arrives right after the restart
    await h2.settle()

    incidents = await h2.incidents()
    assert len(incidents) == 3 and all(i["state"] == "diagnosed" for i in incidents)
    assert sum(i["occurrences"] for i in incidents) == 6  # 4 + 1 + 1: no alert lost
    assert len(fakes.telegram_bodies) == 3


async def test_interrupted_analysis_goes_back_to_the_queue(make_harness, fakes):
    h = make_harness()
    await h.post(a())
    await h.orch.tick()
    await h.orch.drain()
    (inc,) = await h.incidents()
    await h.store.set_state(inc["id"], "analysing")  # as if the process died mid-run
    fakes.telegram_bodies.clear()
    await h.orch.recover()
    assert (await h.store.get_incident(inc["id"]))["state"] == "queued"
    await h.settle()
    assert (await h.store.get_incident(inc["id"]))["state"] == "diagnosed" and len(fakes.telegram_bodies) == 1
