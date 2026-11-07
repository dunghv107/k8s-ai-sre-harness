"""Shared fakes: every external dependency (Kubernetes, Loki, OpenAI, Telegram) is a respx route."""
import asyncio
import itertools
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
import httpx2
from openai import AsyncOpenAI

from alert_agent.app import _parse
from alert_agent.config import Settings
from alert_agent.orchestrator import Orchestrator
from alert_agent.store import Store
from alert_agent.telegram import Telegram
from alert_agent.tools.k8s import K8s
from alert_agent.tools.telemetry import Telemetry

FIXTURES = Path(__file__).parent / "fixtures"
# Fake credentials, built from pieces so secret scanners do not flag the repo.
FAKE_OPENAI_KEY = "sk-" + "t" * 40
FAKE_TG_TOKEN = "123456789:" + "A" * 35
FAKE_CHAT_ID = "-100123456"
OPENAI_URL = "https://api.openai.com/v1/responses"
CATALOG_POD = "catalog-api-7d9f8c6b5-abcde"
ERROR_LINE = '{"ts":"2026-11-07T01:12:31Z","level":"error","service":"catalog-api","msg":"TypeError: \'NoneType\' object is not subscriptable","route":"/api/products/{product_id}","stack":"price = product.variants[0].price"}'


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_alert(alertname="Http5xxRate", status="firing", severity="critical", source="prometheus", **labels) -> dict:
    base = {"alertname": alertname, "severity": severity, "source": source, "protected": "false"}
    return {
        "status": status,
        "labels": {**base, **labels},
        "annotations": {"summary": f"{alertname} test alert"},
        "startsAt": "2026-11-07T01:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": uuid.uuid4().hex[:16],
        "generatorURL": "http://kps-prometheus.observability:9090/graph",
    }


def make_settings(tmp_path, **over) -> Settings:
    values = dict(
        agent_engine="native",  # the shipped default is fallback; tests opt in
        db_path=str(tmp_path / "agent.db"),
        openai_api_key=FAKE_OPENAI_KEY,
        telegram_bot_token=FAKE_TG_TOKEN,
        telegram_chat_id=FAKE_CHAT_ID,
        k8s_url="http://k8s.test",
        batch_window_seconds=0,
        hold_recheck_seconds=0,
        run_workers=False,
    )
    values.update(over)
    return Settings(**values)


def _response(output: list, model="gpt-6.1-terra", tokens=(1000, 200)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_" + uuid.uuid4().hex[:12], "object": "response", "created_at": 0, "status": "completed",
            "model": model, "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)},
        },
    )


def tool_call(name: str, args: dict) -> dict:
    cid = "call_" + uuid.uuid4().hex[:8]
    return {"type": "function_call", "id": "fc_" + cid, "call_id": cid, "name": name, "arguments": json.dumps(args), "status": "completed"}


def message(text: str) -> dict:
    return {"type": "message", "id": "msg_" + uuid.uuid4().hex[:8], "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def verdict_json(label="app", confidence="high", evidence=None, **over) -> str:
    v = {
        "label": label, "status": "ongoing", "confidence": confidence,
        "summary": "catalog-api trả 500 vì sản phẩm không có biến thể.",
        "cause": "TypeError khi đọc variants[0].price của sản phẩm 4.",
        "evidence": [{"source": "logs", "ref": CATALOG_POD, "excerpt": "TypeError: 'NoneType' object is not subscriptable"}] if evidence is None else evidence,
        "next_step": "Chuyển cho dev kiểm tra dữ liệu sản phẩm 4.", "dev_message": "",
    }
    v.update(over)
    return json.dumps(v, ensure_ascii=False)


class FakeOpenAI:
    """Scripted Responses API. `policy(body) -> (output_items, model_tokens)`; the default asks for the pod logs, then answers."""

    def __init__(self, router: respx.MockRouter, final=None, delay=0.0):
        self.requests: list[dict] = []
        self.final = final or (lambda body: verdict_json())
        self.policy = self._default
        self.delay = delay
        self.active = self.max_active = 0
        self.route = router.post(OPENAI_URL).mock(side_effect=self._handle)

    def _default(self, body):
        if "previous_response_id" not in body:
            return [tool_call("get_pod_logs", {"namespace": "shop", "pod": CATALOG_POD, "since_minutes": 15, "previous": False, "grep": None})]
        return [message(self.final(body))]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return _response(self.policy(body), model=body.get("model", ""))
        finally:
            self.active -= 1

    @property
    def models(self) -> list[str]:
        return [r["model"] for r in self.requests if "previous_response_id" not in r]


class Fakes:
    def __init__(self, router: respx.MockRouter):
        self.router = router
        self.telegram_bodies: list[dict] = []
        self._ids = itertools.count(100)
        self.rolling = False
        self.pod_age = 3600
        self.pod_ready = True
        self.errors = 5
        self.log_text = ERROR_LINE
        self.events = [{"lastTimestamp": "2026-11-07T01:12:00Z", "type": "Warning", "reason": "Unhealthy", "message": "Readiness probe failed", "count": 2}]
        self.netpols: list[dict] = []
        self.audit_lines: list[str] = []
        self.pods_override: list[dict] | None = None
        router.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(side_effect=self._telegram)
        router.get("http://k8s.test/api/v1/namespaces/shop/pods").mock(side_effect=self._pods)
        router.get(url__regex=r"http://k8s\.test/api/v1/namespaces/shop/pods/[^/]+/log").mock(side_effect=lambda r: httpx.Response(200, text=self.log_text))
        router.get("http://k8s.test/api/v1/namespaces/shop/events").mock(side_effect=self._events)
        router.get(url__regex=r"http://k8s\.test/apis/apps/v1/namespaces/shop/deployments/.+").mock(side_effect=self._deployment)
        router.get("http://k8s.test/apis/apps/v1/namespaces/shop/replicasets").mock(side_effect=self._replicasets)
        router.get("http://loki.observability:3100/loki/api/v1/query").mock(side_effect=self._loki_count)
        router.get("http://k8s.test/apis/networking.k8s.io/v1/namespaces/shop/networkpolicies").mock(side_effect=lambda r: httpx.Response(200, json={"items": self.netpols}))
        router.get("http://loki.observability:3100/loki/api/v1/query_range").mock(side_effect=self._loki_range)
        self.openai = FakeOpenAI(router)

    def _telegram(self, request):
        self.telegram_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": next(self._ids)}})

    def _loki_range(self, request):
        values = [[str(1_794_000_000_000_000_000 + i * 1_000_000_000), line] for i, line in enumerate(self.audit_lines)]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": [{"stream": {"job": "k8s-audit"}, "values": values}] if values else []}})

    def _pods(self, request):
        if self.pods_override is not None:
            return httpx.Response(200, json={"items": self.pods_override})
        created = (datetime.now(timezone.utc) - timedelta(seconds=self.pod_age)).isoformat()
        pod = {
            "metadata": {"name": CATALOG_POD, "creationTimestamp": created},
            "status": {"phase": "Running", "containerStatuses": [{"ready": self.pod_ready, "restartCount": 0 if self.pod_ready else 2, "state": {}, "lastState": {}}]},
        }
        return httpx.Response(200, json={"items": [pod]})

    def _events(self, request):
        return httpx.Response(200, json={"items": self.events})

    def _deployment(self, request):
        n = 2
        status = {"observedGeneration": 6 if self.rolling else 7, "updatedReplicas": 1 if self.rolling else n, "readyReplicas": n}
        return httpx.Response(200, json={
            "metadata": {"generation": 7, "annotations": {"deployment.kubernetes.io/revision": "7"}},
            "spec": {"replicas": n, "selector": {"matchLabels": {"app.kubernetes.io/name": "catalog-api"}},
                     "template": {"spec": {"containers": [{"image": "homelab/catalog-api:0.1.0"}]}}},
            "status": status,
        })

    def _replicasets(self, request):
        return httpx.Response(200, json={"items": [{"metadata": {"name": "catalog-api-7d9f8c6b5", "creationTimestamp": datetime.now(timezone.utc).isoformat(), "annotations": {"deployment.kubernetes.io/revision": "7"}}, "status": {"replicas": 2, "readyReplicas": 2}}]})

    def _loki_count(self, request):
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, str(self.errors)]}]}})


@pytest.fixture
def fakes():
    with respx.mock(assert_all_called=False) as router:
        yield Fakes(router)


class Harness:
    """Real store, tools, Telegram and OpenAI SDK, wired to the fakes."""

    def __init__(self, settings: Settings, fakes: Fakes):
        self.s, self.fakes = settings, fakes
        self.store = Store(settings.db_path)
        self.k8s, self.telemetry, self.telegram = K8s(settings), Telemetry(settings), Telegram(settings)
        self.client = None if settings.demo_mode else openai_client(fakes.router, settings.openai_api_key)
        self.orch = Orchestrator(settings, self.store, self.k8s, self.telemetry, self.telegram, self.client)

    async def post(self, *alerts: dict) -> int:
        """What the webhook does: parse and store."""
        return await self.store.insert_alerts(_parse({"alerts": list(alerts)}))

    async def settle(self, now=None, rounds=20) -> None:
        """Tick until nothing is waiting to be gated, queued or analysed."""
        for _ in range(rounds):
            await self.orch.tick(now)
            await self.orch.drain()
            if not await self.store.incidents_in_state("new", "queued", "analysing", "folded") and not await self.store.unprocessed_alerts():
                return

    async def incidents(self, *states) -> list[dict]:
        return await self.store.incidents_in_state(*(states or ("new", "held", "queued", "analysing", "diagnosed", "resolved", "self_resolved", "reported_only", "folded")))

    async def runs(self) -> list[dict]:
        return await self.store._run(self.store._all, "SELECT * FROM runs ORDER BY id")


@pytest.fixture
def make_harness(tmp_path, fakes):
    created = []

    def build(**over) -> Harness:
        h = Harness(make_settings(tmp_path, **over), fakes)
        created.append(h)
        return h

    yield build
    for h in created:
        h.store.close()


class Httpx2Bridge(httpx2.AsyncBaseTransport):
    """openai 3.x talks through httpx2, which respx does not patch: hand its requests to the respx router."""

    def __init__(self, router: respx.MockRouter):
        self.router = router

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        fake = httpx.Request(request.method, str(request.url), headers=list(request.headers.raw), content=await request.aread())
        reply = await self.router.async_handler(fake)
        return httpx2.Response(reply.status_code, headers=reply.headers.raw, content=reply.content)


def openai_client(router: respx.MockRouter, api_key: str = FAKE_OPENAI_KEY) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, max_retries=0, http_client=httpx2.AsyncClient(transport=Httpx2Bridge(router)))
