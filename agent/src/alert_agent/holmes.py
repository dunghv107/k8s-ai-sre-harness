"""Second engine: ask HolmesGPT (POST /api/chat) and hold its answer to the same verdict contract as the native engine."""
import asyncio
import logging

import httpx

from .config import Settings
from .subagent import SYSTEM_PROMPT, Run, build_prompt, unknown_verdict, validate

log = logging.getLogger("alert_agent.holmes")

# Name of the entry in Holmes' modelList (platform/holmes/values.yaml); without it Holmes falls back to its built-in default model.
HOLMES_MODEL = "terra"


def build_question(incidents: list[dict], context: dict[int, list[dict]], pack: str = "") -> str:
    """Same redacted incident facts the native engine starts from."""
    return build_prompt(incidents, context, pack).replace("Diagnose this incident group:", "Investigate this incident group and give the verdict JSON:", 1)


async def _chat(client: httpx.AsyncClient, settings: Settings, body: dict) -> dict:
    # Holmes copies request headers into tool templates and rejects '*', which httpx sends in its default "Accept: */*".
    headers = {"Accept": "application/json"}
    if settings.holmes_api_key:
        headers["X-API-Key"] = settings.holmes_api_key
    r = await client.post(f"{settings.holmes_url.rstrip('/')}/api/chat", json={**body, "model": HOLMES_MODEL, "stream": False}, headers=headers)
    r.raise_for_status()
    return r.json()


def _count(run: Run, resp: dict) -> None:
    """Usage per request: Holmes reports cumulative tokens of its whole tool loop in metadata.costs."""
    costs = (resp.get("metadata") or {}).get("costs") or {}
    run.input_tokens += int(costs.get("prompt_tokens") or 0)
    run.output_tokens += int(costs.get("completion_tokens") or 0)
    run.tool_calls += len(resp.get("tool_calls") or [])
    run.log += [t.get("tool_name", "?") for t in resp.get("tool_calls") or []]


async def run_holmes(incidents, context, *, settings: Settings, client: httpx.AsyncClient, pack: str = "") -> Run:
    """Never raises: failures come back as a Run with a fallback verdict and the matching outcome."""
    run = Run(verdict=unknown_verdict("chưa chạy"))
    try:
        await asyncio.wait_for(_ask(run, incidents, context, settings, client, pack), settings.subagent_timeout_seconds)
    except asyncio.TimeoutError:
        run.outcome, run.verdict = "timeout", unknown_verdict("hết thời gian phân tích")
    except Exception as e:
        log.warning("holmes run failed: %s", type(e).__name__)
        run.outcome, run.verdict = "error", unknown_verdict(f"lỗi khi gọi HolmesGPT ({type(e).__name__})")
    return run


async def _ask(run: Run, incidents, context, settings: Settings, client: httpx.AsyncClient, pack: str = "") -> None:
    protected = any(i["protected"] for i in incidents)
    system = SYSTEM_PROMPT.format(max_calls=settings.max_tool_calls, namespaces=", ".join(settings.namespaces))
    resp = await _chat(client, settings, {"ask": build_question(incidents, context, pack), "additional_system_prompt": system})
    _count(run, resp)
    try:
        run.verdict = validate(resp.get("analysis") or "", protected)
        return
    except ValueError as first:
        error = str(first)
    repair = {"ask": f"Your answer was rejected: {error}. Reply with only the corrected JSON object.",
              "conversation_history": resp.get("conversation_history"), "additional_system_prompt": system}
    resp = await _chat(client, settings, repair)
    _count(run, resp)
    try:
        run.verdict = validate(resp.get("analysis") or "", protected)
    except ValueError as second:
        run.outcome, run.verdict = "invalid_output", unknown_verdict(f"HolmesGPT trả kết quả không hợp lệ ({second})")
