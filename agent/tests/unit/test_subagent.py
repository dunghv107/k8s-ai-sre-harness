import json

import pytest


from alert_agent.subagent import run_subagent, unknown_verdict, validate
from alert_agent.tools import Toolbox
from alert_agent.tools.k8s import K8s
from alert_agent.tools.telemetry import Telemetry
from conftest import CATALOG_POD, ERROR_LINE, FAKE_OPENAI_KEY, make_settings, message, openai_client, tool_call, verdict_json

SECRET = "sk-" + "y" * 40  # fake, assembled so scanners stay quiet


def incident(**over):
    base = {"id": 1, "rule": "Http5xxRate", "source": "prometheus", "severity": "critical", "protected": 0, "namespace": "shop",
            "service": "catalog-api", "route": "/api/products/{product_id}", "first_seen": "2026-11-07T01:12:30+00:00",
            "last_seen": "2026-11-07T01:13:30+00:00", "occurrences": 2}
    return {**base, **over}


@pytest.fixture
def parts(tmp_path, fakes):
    s = make_settings(tmp_path)
    client = openai_client(fakes.router)
    return s, client, Toolbox(K8s(s), Telemetry(s))


async def run(parts, incidents=None, model="gpt-6.1-terra"):
    s, client, toolbox = parts
    incidents = incidents or [incident()]
    context = {i["id"]: [{"annotations": {"summary": "5xx from catalog-api"}}] for i in incidents}
    return await run_subagent(incidents, context, model, settings=s, client=client, toolbox=toolbox)


async def test_tool_call_then_final_json(parts, fakes):
    result = await run(parts)
    assert result.outcome == "ok" and result.tool_calls == 1
    assert result.verdict["label"] == "app" and result.verdict["evidence"][0]["source"] == "logs"
    first, second = fakes.openai.requests
    assert first["model"] == "gpt-6.1-terra" and first["tools"] and all(t["strict"] for t in first["tools"])
    assert "previous_response_id" not in first and second["previous_response_id"].startswith("resp_")
    out = second["input"][0]
    assert out["type"] == "function_call_output" and out["call_id"].startswith("call_") and "TypeError" in out["output"]
    assert (result.input_tokens, result.output_tokens) == (2000, 400)


async def test_invalid_json_gets_one_repair_turn(parts, fakes):
    answers = iter(["this is not json", verdict_json()])
    fakes.openai.policy = lambda body: [message(next(answers))]
    result = await run(parts)
    assert result.outcome == "ok" and result.verdict["label"] == "app"
    assert len(fakes.openai.requests) == 2
    assert "rejected" in fakes.openai.requests[1]["input"][0]["content"]
    assert fakes.openai.requests[1]["tool_choice"] == "none"


async def test_invalid_twice_falls_back_to_unknown(parts, fakes):
    fakes.openai.policy = lambda body: [message("nope")]
    result = await run(parts)
    assert result.outcome == "invalid_output"
    assert result.verdict["label"] == "unknown" and result.verdict["confidence"] == "low"
    assert result.verdict["summary"].startswith("Chưa đủ bằng chứng")


async def test_empty_evidence_is_forced_to_unknown(parts, fakes):
    fakes.openai.policy = lambda body: [message(verdict_json(label="app", confidence="high", evidence=[]))]
    result = await run(parts)
    assert result.verdict["label"] == "unknown" and result.verdict["confidence"] == "low"
    assert result.verdict["summary"].startswith("Chưa đủ bằng chứng") and result.verdict["evidence"] == []


async def test_security_label_only_for_protected(parts, fakes):
    fakes.openai.policy = lambda body: [message(verdict_json(label="security"))]
    result = await run(parts)  # not protected: rejected, repaired, still rejected
    assert result.outcome == "invalid_output"
    result = await run(parts, [incident(protected=1, source="loki-audit")])
    assert result.outcome == "ok" and result.verdict["label"] == "security"


async def test_secret_in_tool_output_never_reaches_the_model(parts, fakes):
    fakes.log_text = ERROR_LINE + f"\nconnecting with api_key={SECRET} and Authorization: Bearer abcdefghij1234567890XYZ"
    await run(parts)
    body = json.dumps(fakes.openai.requests, ensure_ascii=False)
    assert SECRET not in body and "abcdefghij1234567890XYZ" not in body
    assert "«redacted:" in body


async def test_secret_in_alert_text_never_reaches_the_model(parts, fakes):
    s, client, toolbox = parts
    ctx = {1: [{"annotations": {"summary": f"leak token={SECRET}"}}]}
    await run_subagent([incident()], ctx, "gpt-6.1-terra", settings=s, client=client, toolbox=toolbox)
    assert SECRET not in json.dumps(fakes.openai.requests)


async def test_verdict_text_is_redacted(parts, fakes):
    evidence = [{"source": "logs", "ref": CATALOG_POD, "excerpt": f"password=hunter2hunter2 {SECRET}"}]
    fakes.openai.policy = lambda body: [message(verdict_json(evidence=evidence))]
    result = await run(parts)
    assert "hunter2hunter2" not in json.dumps(result.verdict) and SECRET not in json.dumps(result.verdict)


async def test_tool_budget_is_enforced(parts, fakes):
    calls = {"n": 0}

    def policy(body):
        if body.get("tool_choice") == "none":
            return [message(verdict_json())]
        calls["n"] += 1
        return [tool_call("get_events", {"namespace": "shop", "object_name": CATALOG_POD})]

    fakes.openai.policy = policy
    result = await run(parts)
    assert result.tool_calls == 8 and calls["n"] == 8 and result.outcome == "ok"


async def test_disallowed_namespace_comes_back_as_tool_error(parts, fakes):
    fakes.openai.policy = lambda body: (
        [tool_call("list_pods", {"namespace": "kube-system", "selector": "a=b"})] if "previous_response_id" not in body else [message(verdict_json())]
    )
    await run(parts)
    assert "not allowed" in fakes.openai.requests[1]["input"][0]["output"]


async def test_api_error_becomes_error_outcome(parts, fakes):
    import httpx

    fakes.openai.route.mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    result = await run(parts)
    assert result.outcome == "error" and result.verdict["label"] == "unknown"


async def test_timeout_outcome(tmp_path, fakes):
    s = make_settings(tmp_path, subagent_timeout_seconds=0.05)
    fakes.openai.delay = 1
    client = openai_client(fakes.router)
    result = await run_subagent([incident()], {}, "gpt-6.1-terra", settings=s, client=client, toolbox=Toolbox(K8s(s), Telemetry(s)))
    assert result.outcome == "timeout"


def test_validate_truncates_long_fields():
    v = json.loads(verdict_json(summary="x" * 500, next_step="y" * 500))
    out = validate(json.dumps(v), protected=False)
    assert len(out["summary"]) == 160 and len(out["next_step"]) == 120


def test_unknown_verdict_shape():
    v = unknown_verdict("thiếu log")
    assert v["label"] == "unknown" and v["evidence"] == [] and "thiếu log" in v["summary"]


def test_human_checks_default_empty_and_are_trimmed_redacted_and_capped():
    assert validate(verdict_json(), protected=False)["human_checks"] == []
    checks = [f"việc {i}" for i in range(5)]
    checks[0] = f"  đọc Secret token={SECRET}  "
    out = validate(verdict_json(human_checks=checks + ["", "   "]), protected=False)["human_checks"]
    assert len(out) == 3 and SECRET not in json.dumps(out) and out[1] == "việc 1"
    assert len(validate(verdict_json(human_checks=["y" * 500]), protected=False)["human_checks"][0]) == 120


@pytest.mark.parametrize("bad", ["exec vào pod", [1, 2], {"a": "b"}])
def test_human_checks_must_be_a_list_of_strings(bad):
    with pytest.raises(ValueError, match="human_checks"):
        validate(verdict_json(human_checks=bad), protected=False)


def test_new_evidence_sources_are_accepted():
    for source in ("pod", "audit", "netpol"):
        ev = [{"source": source, "ref": "x", "excerpt": "y"}]
        assert validate(verdict_json(evidence=ev), protected=False)["evidence"][0]["source"] == source


def test_prompt_has_decision_rules_and_pack_goes_into_the_first_message():
    from alert_agent.subagent import SYSTEM_PROMPT, build_prompt

    for needle in ("OOMKilled", "exit 137", "NetworkPolicy was written", "human_checks", "k8s-audit", "unknown"):
        assert needle in SYSTEM_PROMPT
    text = build_prompt([incident()], {1: []}, pack="[pods]\nfoo")
    assert "Context pack" in text and "[pods]\nfoo" in text and "Context pack" not in build_prompt([incident()], {1: []})


def test_exit_137_is_not_oom_without_the_oomkilled_reason():
    from alert_agent.subagent import SYSTEM_PROMPT

    assert "only that exact reason" in SYSTEM_PROMPT and "exit 137 alone is just SIGKILL" in SYSTEM_PROMPT
    assert "liveness" in SYSTEM_PROMPT and "not memory" in SYSTEM_PROMPT
