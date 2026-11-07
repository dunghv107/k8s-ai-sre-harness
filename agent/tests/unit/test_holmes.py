import json

import httpx
import pytest
import respx

from alert_agent.config import Settings
from alert_agent.holmes import run_holmes
from conftest import verdict_json

URL = "http://holmes.test/api/chat"
SECRET = "sk-" + "h" * 40  # fake, assembled so scanners stay quiet
INCIDENT = {
    "id": 7, "rule": "Http5xxRate", "source": "prometheus", "severity": "critical", "protected": 0, "namespace": "shop",
    "service": "catalog-api", "route": "/api/products/{product_id}", "first_seen": "2026-11-07T01:00:00+00:00",
    "last_seen": "2026-11-07T01:02:00+00:00", "occurrences": 3,
}
CONTEXT = {7: [{"annotations": {"summary": f"5xx on catalog-api token={SECRET}", "description": ""}}]}


def holmes_json(analysis: str, tools=3, prompt=30000, completion=500) -> httpx.Response:
    return httpx.Response(200, json={
        "analysis": analysis, "conversation_history": [{"role": "assistant", "content": analysis}],
        "tool_calls": [{"tool_name": f"tool{i}"} for i in range(tools)],
        "metadata": {"costs": {"prompt_tokens": prompt, "completion_tokens": completion, "total_cost": 0.0325}},
    })


def settings(**over) -> Settings:
    return Settings(holmes_url="http://holmes.test", holmes_api_key="k" * 12, agent_engine="holmes", **over)


async def ask(s=None):
    async with httpx.AsyncClient() as client:
        return await run_holmes([INCIDENT], CONTEXT, settings=s or settings(), client=client)


async def test_valid_answer_gives_verdict_usage_and_a_redacted_request():
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=holmes_json(verdict_json()))
        run = await ask()
    assert run.outcome == "ok" and run.verdict["label"] == "app"
    assert (run.tool_calls, run.input_tokens, run.output_tokens) == (3, 30000, 500)
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["model"] == "terra" and body["stream"] is False
    assert "catalog-api" in body["ask"] and "Http5xxRate" in body["ask"] and SECRET not in body["ask"]
    assert "Final answer" in body["additional_system_prompt"] and "Vietnamese" in body["additional_system_prompt"]
    assert request.headers["x-api-key"] == "k" * 12 and request.headers["accept"] == "application/json"  # Holmes rejects '*' in headers


async def test_answer_is_redacted_before_it_is_kept():
    leaky = verdict_json(cause=f"Khoá {SECRET} bị lộ trong log.")
    with respx.mock() as router:
        router.post(URL).mock(return_value=holmes_json(leaky))
        run = await ask()
    assert SECRET not in json.dumps(run.verdict)


async def test_fenced_json_is_accepted():
    with respx.mock() as router:
        router.post(URL).mock(return_value=holmes_json("```json\n" + verdict_json() + "\n```"))
        assert (await ask()).outcome == "ok"


async def test_invalid_answer_gets_one_repair_turn_with_the_conversation():
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=[holmes_json("not json at all"), holmes_json(verdict_json(), tools=0, prompt=8000, completion=300)])
        run = await ask()
    assert run.outcome == "ok" and len(route.calls) == 2
    repair = json.loads(route.calls[1].request.content)
    assert "rejected" in repair["ask"] and repair["conversation_history"] == [{"role": "assistant", "content": "not json at all"}]
    assert (run.tool_calls, run.input_tokens, run.output_tokens) == (3, 38000, 800)  # both turns are billed


async def test_invalid_twice_ends_as_unknown():
    with respx.mock() as router:
        router.post(URL).mock(return_value=holmes_json("still not json"))
        run = await ask()
    assert run.outcome == "invalid_output" and run.verdict["label"] == "unknown"


async def test_http_error_ends_as_error_without_raising():
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(500))
        run = await ask()
    assert run.outcome == "error" and run.verdict["confidence"] == "low"


def test_engine_setting_is_validated():
    assert Settings.from_env({}).agent_engine == "fallback"
    assert Settings.from_env({"AGENT_ENGINE": "Both", "HOLMES_URL": "http://h", "HOLMES_API_KEY": "x"}).agent_engine == "both"
    with pytest.raises(ValueError):
        Settings.from_env({"AGENT_ENGINE": "gpt"})
