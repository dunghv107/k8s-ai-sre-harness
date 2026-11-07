"""AGENT_ENGINE=fallback: native first, HolmesGPT once as backup, one final message; out-of-scope incidents never reach a model."""
import json

import httpx

from conftest import load_fixture, make_alert, verdict_json

HOLMES = "http://holmes.test/api/chat"


def holmes_answer(**over):
    text = verdict_json(label="infra", summary="HolmesGPT: pod bị OOMKilled.", **over)
    return lambda request: httpx.Response(200, json={
        "analysis": text, "conversation_history": [], "tool_calls": [{"tool_name": "kubernetes_jq_query"}] * 3,
        "metadata": {"costs": {"prompt_tokens": 40000, "completion_tokens": 600, "total_cost": 0.043}},
    })


def setup(make_harness, fakes, answer=None, **over):
    route = fakes.router.post(HOLMES).mock(side_effect=answer or holmes_answer())
    return make_harness(agent_engine="fallback", holmes_url="http://holmes.test", **over), route


async def run_5xx(h):
    await h.post(*load_fixture("am_5xx_firing.json")["alerts"])
    await h.settle()


async def test_native_high_confidence_never_calls_holmes(make_harness, fakes):
    h, route = setup(make_harness, fakes)
    await run_5xx(h)
    assert route.call_count == 0 and fakes.openai.models == ["gpt-6.1-terra"]
    (text,) = [b["text"] for b in fakes.telegram_bodies]
    assert "[native] Chẩn đoán" in text
    assert [r["engine"] for r in await h.runs()] == ["native"]


async def test_native_low_confidence_asks_holmes_once_and_posts_one_message(make_harness, fakes):
    h, route = setup(make_harness, fakes)
    fakes.openai.final = lambda body: verdict_json(label="unknown", confidence="low", evidence=[])
    await run_5xx(h)
    assert route.call_count == 1
    assert fakes.openai.models == ["gpt-6.1-terra"]  # no Sol re-run any more
    (text,) = [b["text"] for b in fakes.telegram_bodies]
    assert "[holmes] Chẩn đoán" in text and "gpt-6.1-terra → holmes/gpt-6.1-terra" in text and "infra" in text
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and json.loads(inc["verdict"])["label"] == "infra"
    runs = await h.runs()
    assert [r["engine"] for r in runs] == ["native", "holmes"] and runs[1]["cost_usd"] == (40000 + 600 * 5) / 1e6
    assert await h.store.cost_today(h.s.timezone) > 0.04  # both runs count toward the cap


async def test_low_confidence_with_label_also_falls_back(make_harness, fakes):
    h, route = setup(make_harness, fakes)
    fakes.openai.final = lambda body: verdict_json(confidence="low")
    await run_5xx(h)
    assert route.call_count == 1 and "[holmes]" in fakes.telegram_bodies[0]["text"]


async def test_holmes_not_better_keeps_native_verdict(make_harness, fakes):
    unknown = verdict_json(label="unknown", confidence="low", evidence=[])
    h, route = setup(make_harness, fakes, answer=lambda r: httpx.Response(200, json={"analysis": unknown, "tool_calls": [], "metadata": {"costs": {}}}))
    fakes.openai.final = lambda body: verdict_json(label="app", confidence="low")
    await run_5xx(h)
    assert route.call_count == 1 and len(fakes.telegram_bodies) == 1 and "[native]" in fakes.telegram_bodies[0]["text"]
    (inc,) = await h.incidents()
    assert json.loads(inc["verdict"])["label"] == "app"


async def test_dead_holmes_keeps_native_verdict(make_harness, fakes):
    h, route = setup(make_harness, fakes, answer=lambda r: httpx.Response(503))
    fakes.openai.final = lambda body: verdict_json(confidence="low")
    await run_5xx(h)
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and len(fakes.telegram_bodies) == 1 and "[native]" in fakes.telegram_bodies[0]["text"]
    assert {r["engine"]: r["outcome"] for r in await h.runs()} == {"native": "ok", "holmes": "error"}


async def test_holmes_is_skipped_when_the_cost_cap_is_reached(make_harness, fakes):
    h, route = setup(make_harness, fakes, daily_cost_cap_usd=0.001)
    fakes.openai.final = lambda body: verdict_json(confidence="low")
    await run_5xx(h)  # native costs 0.004: over the cap before Holmes
    assert route.call_count == 0 and len(fakes.telegram_bodies) == 1 and "[native]" in fakes.telegram_bodies[0]["text"]


async def test_protected_incident_still_uses_sol_then_holmes_when_weak(make_harness, fakes):
    h, route = setup(make_harness, fakes)
    fakes.openai.final = lambda body: verdict_json(label="unknown", confidence="low", evidence=[])
    await h.post(*load_fixture("am_audit_secret.json")["alerts"])
    await h.settle()
    assert fakes.openai.models == ["gpt-6.1-sol"] and route.call_count == 1


async def test_first_model_message_carries_the_context_pack(make_harness, fakes):
    h, _ = setup(make_harness, fakes)
    fakes.netpols = [{"metadata": {"name": "postgres-ingress"}, "spec": {"podSelector": {"matchLabels": {"app": "postgres"}}}}]
    await run_5xx(h)
    first = fakes.openai.requests[0]["input"][0]["content"]
    assert "Context pack" in first and "[network policies]" in first and "postgres-ingress" in first and "[audit]" in first
    assert "human_checks" in fakes.openai.requests[0]["instructions"]


async def test_holmes_receives_the_pack_too(make_harness, fakes):
    h, route = setup(make_harness, fakes)
    fakes.openai.final = lambda body: verdict_json(confidence="low")
    await run_5xx(h)
    assert "Context pack" in json.loads(route.calls.last.request.content)["ask"]


async def test_out_of_scope_incident_is_reported_only_without_a_model_call(make_harness, fakes):
    h, route = setup(make_harness, fakes)
    await h.post(make_alert(alertname="PodOOMKilled", namespace="observability", service="tempo-0", severity="warning"))
    await h.settle()
    (inc,) = await h.incidents()
    assert inc["state"] == "reported_only" and "ngoài phạm vi" in inc["gate_reason"]
    assert fakes.openai.route.call_count == 0 and route.call_count == 0 and await h.runs() == []
    (text,) = [b["text"] for b in fakes.telegram_bodies]
    assert "observability" in text and "Chưa chẩn đoán" in text


async def test_human_checks_are_rendered(make_harness, fakes):
    h, _ = setup(make_harness, fakes)
    fakes.openai.final = lambda body: verdict_json(human_checks=["Exec vào pod xem /proc/meminfo", "Đọc Secret db-credentials", "<b>x</b>"])
    await run_5xx(h)
    text = fakes.telegram_bodies[0]["text"]
    assert "Chỉ người làm được:" in text and "• Exec vào pod xem /proc/meminfo" in text and "• &lt;b&gt;x&lt;/b&gt;" in text
    (inc,) = await h.incidents()
    assert len(json.loads(inc["verdict"])["human_checks"]) == 3


async def test_every_run_stores_its_own_verdict(make_harness, fakes):
    h, _ = setup(make_harness, fakes)
    fakes.openai.final = lambda body: verdict_json(label="unknown", confidence="low", evidence=[])
    await run_5xx(h)
    native, holmes = await h.runs()
    assert json.loads(native["verdict"])["label"] == "unknown" and json.loads(holmes["verdict"])["label"] == "infra"


def test_old_runs_table_gets_the_verdict_column(tmp_path):
    import sqlite3

    from alert_agent.store import Store

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, incident_id INTEGER, model TEXT, started_at TEXT, finished_at TEXT, tool_calls INTEGER,"
                " input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, outcome TEXT)")
    con.commit()
    con.close()
    store = Store(str(db))
    assert "verdict" in {r["name"] for r in store._conn.execute("PRAGMA table_info(runs)")}
    store.close()
