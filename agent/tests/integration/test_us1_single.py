import json

import pytest

from conftest import ERROR_LINE, FAKE_OPENAI_KEY, FAKE_TG_TOKEN, load_fixture, make_alert, message, verdict_json

SECRET = "sk-" + "w" * 40  # fake, assembled so scanners stay quiet


async def post_fixture(h, name):
    return await h.post(*load_fixture(name)["alerts"])


async def test_single_5xx_gets_one_diagnosis_and_a_run_row(make_harness, fakes):
    h = make_harness()
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()

    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and inc["message_id"] and inc["occurrences"] == 1
    assert len(fakes.telegram_bodies) == 1
    text = fakes.telegram_bodies[0]["text"]
    assert "🧠 <b>Chẩn đoán #1" in text and "catalog-api" in text and "Http5xxRate" in text
    assert "🏷️ app · ongoing · tin cậy high" in text
    assert "📎 logs: TypeError: &#x27;NoneType&#x27; object is not subscriptable" in text  # HTML-escaped log line
    assert fakes.telegram_bodies[0]["parse_mode"] == "HTML" and fakes.telegram_bodies[0]["chat_id"] == "-100123456"
    verdict = json.loads(inc["verdict"])
    assert verdict["label"] == "app" and verdict["evidence"][0]["excerpt"].startswith("TypeError")

    (run,) = await h.runs()
    assert run["model"] == "gpt-6.1-terra" and run["outcome"] == "ok" and run["tool_calls"] == 1
    assert (run["input_tokens"], run["output_tokens"]) == (2000, 400)
    assert run["cost_usd"] == pytest.approx((2000 * 1 + 400 * 5) / 1e6)
    assert "0.0040 USD" in text


async def test_resolved_notification_moves_incident_to_resolved(make_harness, fakes):
    h = make_harness()
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    await post_fixture(h, "am_5xx_resolved.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "resolved"
    assert len(fakes.telegram_bodies) == 1  # a resolve is silent in the MVP


async def test_firing_again_after_resolve_opens_a_new_incident(make_harness, fakes):
    h = make_harness()
    for name in ("am_5xx_firing.json", "am_5xx_resolved.json", "am_5xx_firing.json"):
        await post_fixture(h, name)
        await h.settle()
    incidents = await h.incidents()
    assert [i["id"] for i in incidents] == [1, 2] and [i["state"] for i in incidents] == ["resolved", "diagnosed"]
    assert len(fakes.telegram_bodies) == 2


async def test_marker_never_opens_an_incident(make_harness, fakes):
    h = make_harness()
    await post_fixture(h, "am_rollout_marker.json")
    await h.settle()
    assert await h.incidents() == [] and fakes.telegram_bodies == []


async def test_protected_incident_runs_on_escalation_model(make_harness, fakes):
    h = make_harness()
    fakes.openai.final = lambda body: verdict_json(label="security", evidence=[{"source": "logs", "ref": "k8s-audit", "excerpt": "dev-user list secrets"}])
    await post_fixture(h, "am_audit_secret.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and inc["protected"] == 1
    assert fakes.openai.models == ["gpt-6.1-sol"]
    text = fakes.telegram_bodies[0]["text"]
    assert "🔒 Bảo mật — không tự đóng, cần người xác nhận" in text and "gpt-6.1-sol" in text and "security" in text
    # A recovered protected incident is never silently closed by a diagnosis; only a resolve notification changes it.
    assert inc["state"] != "resolved"


async def test_low_confidence_in_native_engine_is_not_rerun(make_harness, fakes):
    h = make_harness()
    fakes.openai.final = lambda body: verdict_json(confidence="low")
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    assert fakes.openai.models == ["gpt-6.1-terra"]
    assert len(await h.runs()) == 1 and len(fakes.telegram_bodies) == 1 and "tin cậy low" in fakes.telegram_bodies[0]["text"]


async def test_low_confidence_on_sol_is_final(make_harness, fakes):
    h = make_harness()
    fakes.openai.final = lambda body: verdict_json(confidence="low")
    await post_fixture(h, "am_audit_secret.json")
    await h.settle()
    assert fakes.openai.models == ["gpt-6.1-sol"]


async def test_cost_cap_reports_without_a_model_call(make_harness, fakes):
    h = make_harness(daily_cost_cap_usd=0.0001)
    await h.store.upsert_incident({"key": "old", "state": "resolved", "first_seen": "2026-01-01T00:00:00+00:00", "last_seen": "2026-01-01T00:00:00+00:00"})
    from alert_agent.store import now_iso

    await h.store.record_run({"incident_id": 1, "model": "gpt-6.1-terra", "started_at": now_iso(), "finished_at": now_iso(), "tool_calls": 1,
                              "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.5, "outcome": "ok"})
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    (inc,) = await h.incidents("reported_only")
    assert fakes.openai.route.call_count == 0
    assert "đã chạm trần chi phí" in fakes.telegram_bodies[0]["text"]
    assert [r["outcome"] for r in await h.runs()] == ["ok", "cost_cap"]


async def test_run_cap_stops_a_free_model(make_harness, fakes):
    # Self-hosted model priced at 0 USD: the cost cap never trips, the run cap bounds GPU load instead.
    h = make_harness(daily_run_cap=1, model_prices=(("gpt-6.1-terra", 0.0, 0.0),))
    from alert_agent.store import now_iso

    await h.store.upsert_incident({"key": "old", "state": "resolved", "first_seen": "2026-01-01T00:00:00+00:00", "last_seen": "2026-01-01T00:00:00+00:00"})
    await h.store.record_run({"incident_id": 1, "model": "gpt-6.1-terra", "started_at": now_iso(), "finished_at": now_iso(), "tool_calls": 1,
                              "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0, "outcome": "ok"})
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    assert fakes.openai.route.call_count == 0
    assert "trần số lần chạy" in fakes.telegram_bodies[0]["text"]
    assert [r["outcome"] for r in await h.runs()] == ["ok", "run_cap"]


async def test_demo_mode_makes_no_openai_or_telegram_calls(make_harness, fakes):
    h = make_harness(demo_mode=True, openai_api_key="", telegram_bot_token="", telegram_chat_id="")
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and "demo" in json.loads(inc["verdict"])["summary"].lower()
    assert fakes.openai.route.call_count == 0 and fakes.telegram_bodies == []
    assert not [c for c in fakes.router.calls if c.request.url.host in ("api.openai.com", "api.telegram.org")]


async def test_openai_failure_is_reported_not_lost(make_harness, fakes):
    import httpx

    h = make_harness()
    fakes.openai.route.mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    await post_fixture(h, "am_5xx_firing.json")
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "reported_only" and (await h.runs())[0]["outcome"] == "error"
    assert "Chưa đủ bằng chứng" in fakes.telegram_bodies[0]["text"]


async def test_seeded_secret_never_leaves_the_process(make_harness, fakes):
    """SC-006: secrets in logs, alert text and even in the verdict reach neither OpenAI, Telegram nor SQLite."""
    h = make_harness()
    fakes.log_text = ERROR_LINE + f"\napi_key={SECRET} password=hunter2hunter2 contact alice.nguyen@example.com"
    leaky = [{"source": "logs", "ref": "catalog-api", "excerpt": f"saw token={SECRET} and {FAKE_TG_TOKEN}"}]
    fakes.openai.final = lambda body: verdict_json(evidence=leaky, cause=f"clé {SECRET}")
    alert = make_alert(namespace="shop", service="catalog-api", http_route="/x")
    alert["annotations"] = {"summary": f"boom password=hunter2hunter2 {SECRET}"}
    await h.post(alert)
    await h.settle()

    assert fakes.telegram_bodies and fakes.openai.requests
    wire = json.dumps(fakes.openai.requests, ensure_ascii=False) + json.dumps(fakes.telegram_bodies, ensure_ascii=False)
    db = json.dumps(await h.incidents()) + json.dumps(await h.store._run(h.store._all, "SELECT * FROM alerts"))
    for needle in (SECRET, "hunter2hunter2", "alice.nguyen@example.com", FAKE_TG_TOKEN, FAKE_OPENAI_KEY):
        assert needle not in wire and needle not in db
    assert "«redacted:" in wire
