"""AGENT_ENGINE=holmes|both: the second engine runs through the same pipeline; the incident's verdict is the native one in `both`."""
import json

import httpx

from conftest import load_fixture, verdict_json

HOLMES = "http://holmes.test/api/chat"


def holmes_answer(request=None) -> httpx.Response:
    text = verdict_json(label="infra", summary="HolmesGPT: lỗi hạ tầng giả định.")
    return httpx.Response(200, json={
        "analysis": text, "conversation_history": [], "tool_calls": [{"tool_name": "kubernetes_jq_query"}] * 4,
        "metadata": {"costs": {"prompt_tokens": 40000, "completion_tokens": 600, "total_cost": 0.0430}},
    })


def engine(make_harness, fakes, kind, answer=holmes_answer, **over):
    route = fakes.router.post(HOLMES).mock(side_effect=answer)
    return make_harness(agent_engine=kind, holmes_url="http://holmes.test", **over), route


async def run_once(h):
    await h.post(*load_fixture("am_5xx_firing.json")["alerts"])
    await h.settle()


async def test_both_posts_two_tagged_messages_and_keeps_the_native_verdict(make_harness, fakes):
    h, route = engine(make_harness, fakes, "both")
    await run_once(h)

    texts = sorted(b["text"] for b in fakes.telegram_bodies)
    assert len(texts) == 2 and "[holmes] Chẩn đoán" in texts[0] and "[native] Chẩn đoán" in texts[1]
    assert "holmes/gpt-6.1-terra" in texts[0] and "0.0430 USD" in texts[0] and "s · 0.0430" in texts[0]
    assert "gpt-6.1-terra · " in texts[1]
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and json.loads(inc["verdict"])["label"] == "app"  # native, not Holmes' "infra"

    runs = {r["engine"]: r for r in await h.runs()}
    assert set(runs) == {"native", "holmes"}
    assert runs["holmes"]["tool_calls"] == 4 and (runs["holmes"]["input_tokens"], runs["holmes"]["output_tokens"]) == (40000, 600)
    assert runs["holmes"]["cost_usd"] == (40000 + 600 * 5) / 1e6 and runs["holmes"]["duration_seconds"] is not None
    assert runs["native"]["duration_seconds"] is not None and route.call_count == 1


async def test_both_survives_a_dead_holmes(make_harness, fakes):
    h, _ = engine(make_harness, fakes, "both", answer=lambda request: httpx.Response(503))
    await run_once(h)

    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and json.loads(inc["verdict"])["label"] == "app"
    texts = [b["text"] for b in fakes.telegram_bodies]
    assert any("[native]" in t for t in texts) and any("[holmes]" in t and "Chưa đủ bằng chứng" in t for t in texts)
    assert {r["engine"]: r["outcome"] for r in await h.runs()} == {"native": "ok", "holmes": "error"}


async def test_holmes_only_never_calls_openai(make_harness, fakes):
    h, route = engine(make_harness, fakes, "holmes")
    await run_once(h)

    assert fakes.openai.requests == [] and route.call_count == 1
    (inc,) = await h.incidents()
    assert inc["state"] == "diagnosed" and json.loads(inc["verdict"])["label"] == "infra"
    assert len(fakes.telegram_bodies) == 1 and "holmes/gpt-6.1-terra" in fakes.telegram_bodies[0]["text"]


async def test_native_engine_never_calls_holmes(make_harness, fakes):
    route = fakes.router.post(HOLMES).mock(side_effect=holmes_answer)
    h = make_harness()
    await run_once(h)
    assert route.call_count == 0 and len(fakes.telegram_bodies) == 1 and "[native]" not in fakes.telegram_bodies[0]["text"]


async def test_holmes_spend_counts_toward_the_daily_cap(make_harness, fakes):
    h, _ = engine(make_harness, fakes, "both")
    await run_once(h)
    # Native cost 0.004 plus Holmes 0.043: both rows are summed by the cap check.
    assert await h.store.cost_today(h.s.timezone) > 0.04
