"""Pipeline driver: intake, batching window, gate, hold loop, severity queue, bounded parallel sub-agents, cost cap."""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import gate, intake
from .config import Settings, cost_usd
from .holmes import run_holmes
from .pack import build_pack
from .render import render_cost_cap, render_diagnosis, render_out_of_scope, render_run_cap
from .store import Store, now_iso, parse_iso
from .subagent import Run, run_subagent, unknown_verdict
from .telegram import Telegram
from .tools import Toolbox
from .tools.k8s import K8s
from .tools.telemetry import Telemetry

log = logging.getLogger("alert_agent.orchestrator")

DEMO_VERDICT = {
    "label": "unknown", "status": "ongoing", "confidence": "low",
    "summary": "Chế độ demo: chưa gọi mô hình, chỉ ghi nhận sự cố.", "cause": "", "evidence": [],
    "next_step": "Tắt AGENT_DEMO_MODE để chẩn đoán thật.", "human_checks": [], "dev_message": "",
}


CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _score(verdict: dict) -> tuple[bool, int]:
    """Orders verdicts: a decided label beats unknown, then higher confidence."""
    return verdict["label"] != "unknown", CONFIDENCE_RANK[verdict["confidence"]]


def _covers(lead: dict, group: list[dict], inc: dict) -> bool:
    """Protected duplicates (same rule and actor) always fold; others only when their service is in the group or named in the verdict."""
    if inc["protected"]:
        return True
    services = {g["service"] for g in group if g["service"]}
    return bool(inc["service"]) and (inc["service"] in services or inc["service"] in (lead["verdict"] or ""))


def _weak(verdict: dict) -> bool:
    return verdict["label"] == "unknown" or verdict["confidence"] == "low"


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, k8s: K8s, telemetry: Telemetry, telegram: Telegram, openai_client=None, holmes_client=None):
        self.s, self.store, self.k8s, self.telemetry, self.telegram = settings, store, k8s, telemetry, telegram
        self.client = openai_client
        # The Holmes client is only built when the engine needs it; its timeout is the sub-agent budget plus a margin for the repair turn.
        self.holmes_client = holmes_client or (httpx.AsyncClient(timeout=settings.subagent_timeout_seconds + 30) if settings.agent_engine != "native" else None)
        self.toolbox = Toolbox(k8s, telemetry)
        self.running: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        if self.holmes_client:
            await self.holmes_client.aclose()

    async def recover(self) -> None:
        """After a restart, incidents that were mid-analysis go back to the queue (SC-010)."""
        n = await self.store.requeue_analysing()
        if n:
            log.info("re-queued %d incidents interrupted by a restart", n)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(self.s.poll_interval_seconds)

    async def drain(self) -> None:
        """Wait for sub-agents that are running now (tests and shutdown)."""
        while self.running:
            await asyncio.gather(*list(self.running), return_exceptions=True)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        await intake.process(self.store, self.s)
        await self._gate_pass(now)
        await self._fold_pass(now)
        await self._dispatch()

    # --- gate and hold loop ---
    async def _evaluate(self, inc: dict, now: datetime) -> gate.Decision:
        try:
            return await gate.evaluate(inc, settings=self.s, store=self.store, k8s=self.k8s, telemetry=self.telemetry, now=now)
        except Exception:
            log.exception("gate failed for incident %s, queueing it", inc["id"])
            return gate.Decision("queue", "lỗi cửa lọc, phân tích luôn")

    async def _gate_pass(self, now: datetime) -> None:
        # shortcut: incidents are gated one by one; cache per service if storms of hundreds of distinct services appear.
        for inc in await self.store.incidents_in_state("new"):
            if now - parse_iso(inc["first_seen"]) < timedelta(seconds=self.s.batch_window_seconds):
                continue
            await self._apply(inc, await self._evaluate(inc, now), now)
        for inc in await self.store.incidents_in_state("held"):
            checked = parse_iso(inc["gate_checked_at"]) if inc["gate_checked_at"] else None
            if checked and now - checked < timedelta(seconds=self.s.hold_recheck_seconds):
                continue
            decision = await self._evaluate(inc, now)
            if decision.action == "hold" and now - parse_iso(inc["held_at"]) >= timedelta(seconds=self.s.hold_max_seconds):
                if decision.kind == "below_threshold":
                    decision = gate.Decision("reported_only", f"{decision.reason}; chỉ ghi nhận")
                else:  # a real bug is diagnosed late, never missed
                    decision = gate.Decision("queue", f"hết hạn hoãn ({decision.reason})")
            await self._apply(inc, decision, now)

    async def _apply(self, inc: dict, d: gate.Decision, now: datetime) -> None:
        extra = {"gate_reason": d.reason, "gate_checked_at": now_iso(now)}
        if d.action == "hold":
            await self.store.set_state(inc["id"], "held", held_at=inc["held_at"] or now_iso(now), **extra)
        else:
            state = {"queue": "queued"}.get(d.action, d.action)
            await self.store.set_state(inc["id"], state, **extra)
        log.info("gate incident #%s: %s (%s)", inc["id"], d.action, d.reason)

    # --- folding of late duplicates ---
    async def _fold_pass(self, now: datetime) -> None:
        """A folded incident waits for its group: it adopts the verdict when the group covers it, else it gets its own run."""
        for inc in await self.store.incidents_in_state("folded"):
            group = [g for g in await self.store.incidents_in_state("analysing", "diagnosed", "reported_only", "resolved") if g["correlation_id"] == inc["correlation_id"] and g["id"] != inc["id"]]
            if any(g["state"] == "analysing" for g in group) and now - parse_iso(inc["first_seen"]) < timedelta(seconds=self.s.hold_max_seconds):
                continue
            lead = next((g for g in group if g["state"] in ("diagnosed", "resolved") and g["verdict"]), None)
            if lead and _covers(lead, group, inc):
                await self.store.set_state(inc["id"], "diagnosed", expect="folded", verdict=lead["verdict"], message_id=lead["message_id"],
                                           gate_reason=f"gộp vào chẩn đoán #{lead['id']}: cùng sự cố")
            else:
                await self.store.set_state(inc["id"], "queued", expect="folded", correlation_id=inc["id"], gate_reason="nhóm trước không bao phủ: chạy riêng")
            log.info("fold incident #%s -> %s", inc["id"], "folded" if lead else "own run")

    # --- dispatch ---
    async def _dispatch(self) -> None:
        free = self.s.max_parallel_subagents - len(self.running)
        if free <= 0:
            return
        groups: dict[int, list[dict]] = {}
        for inc in await self.store.incidents_in_state("queued"):
            groups.setdefault(inc["correlation_id"] or inc["id"], []).append(inc)
        rank = intake.SEVERITY_RANK
        ordered = sorted(groups.values(), key=lambda g: (-max(rank.get(i["severity"], 1) for i in g), min(i["first_seen"] for i in g)))
        for group in ordered[:free]:
            claimed = [i for i in group if await self.store.set_state(i["id"], "analysing", expect="queued")]
            if claimed:
                task = asyncio.create_task(self._run_group([i["id"] for i in claimed]))
                self.running.add(task)
                task.add_done_callback(self.running.discard)

    async def _run_group(self, ids: list[int]) -> None:
        try:
            await self._analyse(ids)
        except Exception:
            log.exception("analysis of incidents %s failed", ids)
            for i in ids:
                await self.store.set_state(i, "reported_only", expect="analysing", gate_reason="lỗi nội bộ khi phân tích")

    async def _analyse(self, ids: list[int]) -> None:
        incidents = [await self.store.get_incident(i) for i in ids]
        if self.s.demo_mode:
            run = Run(verdict=DEMO_VERDICT)
            await self._record(incidents[0], "demo", run, 0.0)
            return await self._conclude(incidents, run, "demo", 0.0)
        outside = [i for i in incidents if i["namespace"] and i["namespace"] not in self.s.namespaces]
        if outside:  # no tool can read it: report only, no model call
            reason = f"ngoài phạm vi đọc ({', '.join(self.s.namespaces)}): không gọi mô hình"
            await self._finish(outside, None, render_out_of_scope(outside, self.s.namespaces), "reported_only", gate_reason=reason)
            incidents = [i for i in incidents if i not in outside]
            if not incidents:
                return
        context = {i["id"]: await self.store.alerts_for_incident(i["id"]) for i in incidents}
        pack = await build_pack(incidents, self.k8s, self.telemetry)
        log.info("pack incidents=%s chars=%d", [i["id"] for i in incidents], len(pack))
        engine = self.s.agent_engine
        if engine == "native":
            return await self._native_and_report(incidents, context, pack, tag=None)
        if engine == "fallback":
            return await self._fallback(incidents, context, pack)
        if engine == "holmes":
            out = await self._holmes(incidents, context, pack)
            if out is None:  # a daily cap: nothing was diagnosed, report the cap itself
                return await self._finish(incidents, None, (await self._cap(incidents))[1], "reported_only")
            return await self._conclude(incidents, *out[:3], seconds=out[3], tag=None)
        # both: each engine posts as soon as it finishes; the incident's verdict is the native one.
        await asyncio.gather(self._native_and_report(incidents, context, pack, tag="native"), self._holmes_and_post(incidents, context, pack))

    async def _native_and_report(self, incidents: list[dict], context: dict, pack: str, tag: str | None) -> None:
        out = await self._native(incidents, context, pack)
        if out is None:  # cost cap hit before any diagnosis; already reported
            return
        await self._conclude(incidents, *out[:3], seconds=out[3] if tag else None, tag=tag)

    async def _fallback(self, incidents: list[dict], context: dict, pack: str) -> None:
        """Native first; HolmesGPT is asked once, only when native stays unknown or low. One final message, tagged with the engine that produced it."""
        out = await self._native(incidents, context, pack)
        if out is None:
            return
        run, model, cost, seconds = out
        final, label, engine = run, model, "native"
        if run.outcome != "cost_cap" and _weak(run.verdict):
            backup = await self._holmes(incidents, context, pack)  # None when the cost cap stops it
            if backup is not None:
                hrun, hlabel, hcost, hseconds = backup
                cost, seconds, label = cost + hcost, seconds + hseconds, f"{model} → {hlabel}"
                if hrun.outcome == "ok" and _score(hrun.verdict) > _score(run.verdict):
                    final, engine = hrun, "holmes"
        await self._conclude(incidents, final, label, cost, seconds=seconds, tag=engine)

    async def _native(self, incidents: list[dict], context: dict, pack: str):
        """One OpenAI tool loop (Sol for protected incidents); returns (run, model, cost, seconds) or None when the cost cap stopped it."""
        model = self.s.openai_model_escalate if any(i["protected"] for i in incidents) else self.s.openai_model
        capped = await self._cap(incidents)
        if capped:
            outcome, text = capped
            reason = "trần chi phí" if outcome == "cost_cap" else "trần số lần chạy"
            await self._record(incidents[0], model, Run(verdict=unknown_verdict(reason), outcome=outcome), 0.0)
            await self._finish(incidents, None, text, "reported_only")
            return None
        started = time.monotonic()
        run = await run_subagent(incidents, context, model, settings=self.s, client=self.client, toolbox=self.toolbox, pack=pack)
        took = time.monotonic() - started
        cost = cost_usd(model, run.input_tokens, run.output_tokens, self.s.prices)
        await self._record(incidents[0], model, run, cost, took)
        return run, model, cost, took

    async def _holmes(self, incidents: list[dict], context: dict, pack: str = ""):
        """One HolmesGPT request; returns (run, label, cost, seconds), or None when the cost cap stopped it."""
        if await self._cap(incidents):
            return None
        started = time.monotonic()
        run = await run_holmes(incidents, context, settings=self.s, client=self.holmes_client, pack=pack)
        seconds = time.monotonic() - started
        cost = cost_usd(self.s.openai_model, run.input_tokens, run.output_tokens, self.s.prices)
        await self._record(incidents[0], self.s.openai_model, run, cost, seconds, engine="holmes")
        return run, f"holmes/{self.s.openai_model}", cost, seconds

    async def _cap(self, incidents: list[dict]) -> tuple[str, str] | None:
        """(outcome, message) when a daily cap stops model calls, else None. The run cap matters for a self-hosted model,
        whose cost is 0 USD so the cost cap never trips; it bounds GPU load instead."""
        spent = await self.store.cost_today(self.s.timezone)
        if spent >= self.s.daily_cost_cap_usd:
            return "cost_cap", render_cost_cap(incidents, spent, self.s.daily_cost_cap_usd)
        if self.s.daily_run_cap:
            runs = await self.store.runs_today(self.s.timezone)
            if runs >= self.s.daily_run_cap:
                return "run_cap", render_run_cap(incidents, runs, self.s.daily_run_cap)
        return None

    async def _holmes_and_post(self, incidents: list[dict], context: dict, pack: str = "") -> None:
        try:
            out = await self._holmes(incidents, context, pack)
            if out is not None:
                run, label, cost, seconds = out
                await self._send(render_diagnosis(incidents, run.verdict, label, cost, self.s.timezone, engine="holmes", seconds=seconds))
        except Exception:
            log.exception("holmes engine failed for incidents %s", [i["id"] for i in incidents])

    async def _conclude(self, incidents: list[dict], run: Run, model: str, cost: float, seconds: float | None = None, tag: str | None = None) -> None:
        text = render_diagnosis(incidents, run.verdict, model, cost, self.s.timezone, engine=tag, seconds=seconds)
        state = "reported_only" if run.outcome in ("timeout", "error") else "diagnosed"
        await self._finish(incidents, run.verdict, text, state)

    async def _record(self, inc: dict, model: str, run: Run, cost: float, seconds: float = 0.0, engine: str = "native") -> None:
        finished = datetime.now(timezone.utc)
        # Logged as well as stored: the benchmark reads per-run usage and the redacted verdict of both engines from the pod log.
        log.info("run incident=%s engine=%s model=%s outcome=%s seconds=%.1f tool_calls=%d tokens_in=%d tokens_out=%d cost_usd=%.4f tools=%s",
                 inc["id"], engine, model, run.outcome, seconds, run.tool_calls, run.input_tokens, run.output_tokens, cost, ",".join(run.log))
        log.info("verdict incident=%s engine=%s %s", inc["id"], engine, json.dumps(run.verdict, ensure_ascii=False))
        await self.store.record_run(
            {"incident_id": inc["id"], "model": model, "started_at": now_iso(finished - timedelta(seconds=seconds)), "finished_at": now_iso(finished),
             "tool_calls": run.tool_calls, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens, "cost_usd": cost,
             "outcome": run.outcome, "engine": engine, "duration_seconds": round(seconds, 1),
             "verdict": json.dumps(run.verdict, ensure_ascii=False)}
        )

    async def _send(self, text: str) -> int | None:
        try:
            return await self.telegram.send(text)
        except Exception as e:
            # shortcut: no retry queue for failed posts, the verdict stays in SQLite; add a retry when it happens in practice.
            log.error("telegram send failed: %s", e)
            return None

    async def _finish(self, incidents: list[dict], verdict: dict | None, text: str, state: str, gate_reason: str | None = None) -> None:
        message_id = await self._send(text)
        for inc in incidents:
            fields = {"message_id": message_id}
            if gate_reason:
                fields["gate_reason"] = gate_reason
            if verdict is not None:
                fields["verdict"] = json.dumps(verdict, ensure_ascii=False)
            if await self.store.set_state(inc["id"], state, expect="analysing", **fields):
                if state == "diagnosed" and await self.store.latest_alert_status(inc["id"]) == "resolved":
                    await self.store.set_state(inc["id"], "resolved", expect="diagnosed")
            else:  # resolved while analysing: keep the state, still store the verdict
                await self.store.upsert_incident({"id": inc["id"], **fields})
