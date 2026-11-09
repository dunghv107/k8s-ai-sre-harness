"""LLM_API=chat: the same tool loop over Chat Completions, as served by self-hosted vLLM, Ollama or LiteLLM."""
import json
import uuid

import httpx
import pytest

from alert_agent.subagent import run_subagent, validate
from alert_agent.tools import Toolbox
from alert_agent.tools.k8s import K8s
from alert_agent.tools.telemetry import Telemetry
from conftest import CATALOG_POD, ERROR_LINE, make_settings, openai_client, verdict_json

BASE_URL = "http://llm.internal:8000/v1"
SECRET = "sk-" + "z" * 40  # fake, assembled so scanners stay quiet


def _completion(content=None, calls=(), tokens=(900, 100)) -> httpx.Response:
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(a)}} for n, a in calls]
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "local",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": sum(tokens)},
    })


class FakeChat:
    def __init__(self, router, answers):
        self.requests, self.answers = [], iter(answers)
        router.post(f"{BASE_URL}/chat/completions").mock(side_effect=self._handle)

    def _handle(self, request):
        self.requests.append(json.loads(request.content))
        return next(self.answers)


@pytest.fixture
def parts(tmp_path, fakes):
    s = make_settings(tmp_path, llm_api="chat", openai_base_url=BASE_URL, openai_api_key="")
    client = openai_client(fakes.router)
    client = client.with_options(base_url=BASE_URL)
    return s, client, Toolbox(K8s(s), Telemetry(s))


def incident():
    return {"id": 1, "rule": "Http5xxRate", "source": "prometheus", "severity": "critical", "protected": 0, "namespace": "shop",
            "service": "catalog-api", "route": "/api/products/{product_id}", "first_seen": "2026-11-07T01:12:30+00:00",
            "last_seen": "2026-11-07T01:13:30+00:00", "occurrences": 2}


async def run(parts):
    s, client, toolbox = parts
    return await run_subagent([incident()], {1: []}, "qwen3-30b-a3b", settings=s, client=client, toolbox=toolbox)


LOGS_CALL = ("get_pod_logs", {"namespace": "shop", "pod": CATALOG_POD, "since_minutes": 15, "previous": False, "grep": None})


async def test_tool_call_then_final_json(parts, fakes):
    fake = FakeChat(fakes.router, [_completion(calls=[LOGS_CALL]), _completion(verdict_json())])
    result = await run(parts)
    assert result.outcome == "ok" and result.tool_calls == 1 and result.verdict["label"] == "app"
    assert (result.input_tokens, result.output_tokens) == (1800, 200)
    first, second = fake.requests
    assert first["messages"][0]["role"] == "system" and first["tools"][0]["function"]["name"] == "list_pods"
    # Stateless API: the second turn resends the whole history, ending with the tool result.
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert second["messages"][2]["tool_calls"][0]["id"] == second["messages"][3]["tool_call_id"]
    assert "TypeError" in second["messages"][3]["content"]


async def test_repair_turn_disables_tools(parts, fakes):
    fake = FakeChat(fakes.router, [_completion("not json"), _completion(verdict_json())])
    result = await run(parts)
    assert result.outcome == "ok"
    assert fake.requests[1]["tool_choice"] == "none" and "rejected" in fake.requests[1]["messages"][-1]["content"]


async def test_secret_in_tool_output_never_reaches_the_model(parts, fakes):
    fakes.log_text = ERROR_LINE + f"\napi_key={SECRET}"
    fake = FakeChat(fakes.router, [_completion(calls=[LOGS_CALL]), _completion(verdict_json())])
    await run(parts)
    assert SECRET not in json.dumps(fake.requests)


def test_validate_strips_think_block_and_leading_prose():
    raw = "<think>log shows IndexError</think>\nHere is the verdict: " + verdict_json() + "\n"
    assert validate(raw, protected=False)["label"] == "app"
