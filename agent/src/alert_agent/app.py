"""FastAPI entry point: Alertmanager webhook, health check, background pipeline (contracts/alertmanager-webhook.md)."""
import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .orchestrator import Orchestrator
from .redact import redact
from .store import Store, now_iso
from .telegram import Telegram
from .tools.k8s import K8s
from .tools.telemetry import Telemetry

log = logging.getLogger("alert_agent")


def _redact_map(values: dict) -> dict:
    return {str(k): redact(str(v)) for k, v in values.items()}


def _parse(payload) -> list[dict]:
    """Alertmanager v4 payload to alert rows; raises ValueError when the shape is wrong."""
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        raise ValueError("alerts must be a list")
    received = now_iso()
    rows = []
    for a in payload["alerts"]:
        if not isinstance(a, dict) or a.get("status") not in ("firing", "resolved") or not isinstance(a.get("labels"), dict):
            raise ValueError("each alert needs status firing|resolved and labels")
        annotations = _redact_map(a.get("annotations") or {})
        if a.get("generatorURL"):
            annotations["generatorURL"] = redact(str(a["generatorURL"]))
        rows.append(
            {
                "am_fingerprint": str(a.get("fingerprint", "")),
                "status": a["status"],
                "labels": json.dumps(_redact_map(a["labels"]), ensure_ascii=False),
                "annotations": json.dumps(annotations, ensure_ascii=False),
                "starts_at": a.get("startsAt"),
                "ends_at": a.get("endsAt"),
                "received_at": received,
            }
        )
    return rows


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        for noisy in ("httpx", "httpcore", "openai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)  # their request logs carry URLs, and Telegram's URL holds the token
        missing = settings.missing_credentials()
        if missing:
            raise RuntimeError(f"missing required environment variables: {', '.join(missing)} (or set AGENT_DEMO_MODE=true)")
        store = Store(settings.db_path)
        k8s, telemetry, telegram = K8s(settings), Telemetry(settings), Telegram(settings)
        client = None
        if not settings.demo_mode:
            from openai import AsyncOpenAI

            # The SDK refuses an empty key; a keyless self-hosted endpoint ignores this placeholder.
            client = AsyncOpenAI(api_key=settings.openai_api_key or "unused", base_url=settings.openai_base_url, timeout=60)
        orchestrator = Orchestrator(settings, store, k8s, telemetry, telegram, client)
        await orchestrator.recover()
        app.state.store, app.state.orchestrator = store, orchestrator
        worker = asyncio.create_task(orchestrator.run_forever()) if settings.run_workers else None
        log.info("alert-agent started (demo_mode=%s)", settings.demo_mode)
        try:
            yield
        finally:
            if worker:
                worker.cancel()
            for task in list(orchestrator.running):
                task.cancel()
            await asyncio.gather(*([worker] if worker else []), *orchestrator.running, return_exceptions=True)
            for closer in (k8s.aclose, telemetry.aclose, telegram.aclose, orchestrator.aclose):
                await closer()
            store.close()

    app = FastAPI(title="alert-agent", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        try:
            await request.app.state.store.ping()
        except Exception:
            return JSONResponse({"status": "db unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.post("/alertmanager")
    async def alertmanager(request: Request):
        # Only parse and store: the handler never calls the model.
        try:
            rows = _parse(await request.json())
        except ValueError:  # includes malformed JSON; Alertmanager does not retry 4xx
            return JSONResponse({"error": "malformed alertmanager payload"}, status_code=400)
        try:
            stored = await request.app.state.store.insert_alerts(rows)
        except sqlite3.Error:
            return JSONResponse({"error": "database unavailable"}, status_code=503)  # Alertmanager retries 5xx
        return {"stored": stored}

    return app


app = create_app()
