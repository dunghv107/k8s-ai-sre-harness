from datetime import datetime, timedelta, timezone

from conftest import load_fixture


async def post_fixture(h, name, status=None):
    payload = load_fixture(name)
    for alert in payload["alerts"]:
        alert["status"] = status or alert["status"]
    return await h.post(*payload["alerts"])


def agent_calls(fakes):
    return fakes.openai.route.call_count


async def test_rollout_holds_then_one_run_after_it_completes(make_harness, fakes):
    h = make_harness()
    fakes.rolling = True
    await post_fixture(h, "am_rollout_marker.json")
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()

    (inc,) = await h.incidents()
    assert inc["state"] == "held" and "rollout" in inc["gate_reason"]
    assert agent_calls(fakes) == 0 and fakes.telegram_bodies == []

    await h.settle()  # still rolling: still held, still silent
    assert (await h.incidents())[0]["state"] == "held" and agent_calls(fakes) == 0

    fakes.rolling = False
    await post_fixture(h, "am_rollout_marker.json", status="resolved")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and "qua cửa lọc" in inc["gate_reason"]
    assert len(await h.runs()) == 1 and len(fakes.telegram_bodies) == 1
    assert len(fakes.openai.models) == 1  # exactly one sub-agent


async def test_resolved_during_the_hold_is_self_resolved(make_harness, fakes):
    h = make_harness()
    fakes.rolling = True
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    assert (await h.incidents())[0]["state"] == "held"
    await post_fixture(h, "am_5xx_resolved.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "self_resolved" and "đã hết" in inc["gate_reason"]
    assert agent_calls(fakes) == 0 and fakes.telegram_bodies == [] and await h.runs() == []


async def test_young_failing_pod_is_held_until_it_ages(make_harness, fakes):
    h = make_harness()
    fakes.pod_age, fakes.pod_ready = 40, False
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "held" and "giây tuổi" in inc["gate_reason"] and agent_calls(fakes) == 0
    fakes.pod_age = 400
    await h.settle()
    assert (await h.incidents())[0]["state"] == "diagnosed" and len(await h.runs()) == 1


async def test_tiny_error_burst_is_not_analysed(make_harness, fakes):
    h = make_harness()
    fakes.errors = 1
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "held" and "dưới ngưỡng" in inc["gate_reason"] and agent_calls(fakes) == 0
    fakes.errors = 6  # the errors persist: now it is a real problem
    await h.settle()
    assert (await h.incidents())[0]["state"] == "diagnosed" and len(await h.runs()) == 1


async def test_hold_ceiling_diagnoses_late_instead_of_missing(make_harness, fakes):
    h = make_harness()
    fakes.rolling = True
    await post_fixture(h, "am_5xx_firing.json")
    t0 = datetime.now(timezone.utc)
    await h.settle(now=t0)
    assert (await h.incidents())[0]["state"] == "held"
    await h.settle(now=t0 + timedelta(minutes=11))
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and "hết hạn hoãn" in inc["gate_reason"]
    assert len(await h.runs()) == 1


async def test_protected_incident_ignores_a_rollout(make_harness, fakes):
    h = make_harness()
    fakes.rolling = True
    await post_fixture(h, "am_rollout_marker.json")
    await post_fixture(h, "am_audit_secret.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and len(fakes.telegram_bodies) == 1


async def test_batching_window_delays_the_gate(make_harness, fakes):
    h = make_harness(batch_window_seconds=60)
    await post_fixture(h, "am_5xx_firing.json")
    t0 = datetime.now(timezone.utc)
    await h.orch.tick(t0)
    assert (await h.incidents())[0]["state"] == "new" and agent_calls(fakes) == 0
    await h.settle(now=t0 + timedelta(seconds=61))
    assert (await h.incidents())[0]["state"] == "diagnosed"
