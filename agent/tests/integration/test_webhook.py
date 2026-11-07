import json

import httpx
import pytest

from alert_agent.app import create_app
from conftest import FAKE_OPENAI_KEY, FAKE_TG_TOKEN, load_fixture, make_alert, make_settings

FIXTURE_NAMES = ["am_5xx_firing.json", "am_5xx_resolved.json", "am_audit_secret.json", "am_rollout_marker.json"]


@pytest.fixture
async def client(tmp_path, fakes):
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c:
            c.app = app
            yield c


async def rows(client, sql="SELECT * FROM alerts ORDER BY id"):
    store = client.app.state.store
    return await store._run(store._all, sql)


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_fixture_is_stored(client, fakes, name):
    payload = load_fixture(name)
    r = await client.post("/alertmanager", json=payload)
    assert r.status_code == 200 and r.json() == {"stored": len(payload["alerts"])}
    stored = await rows(client)
    assert len(stored) == len(payload["alerts"])
    first = stored[0]
    assert first["status"] == payload["alerts"][0]["status"]
    assert json.loads(first["labels"])["alertname"] == payload["alerts"][0]["labels"]["alertname"]
    assert first["am_fingerprint"] == payload["alerts"][0]["fingerprint"]
    assert json.loads(first["annotations"])["generatorURL"]
    # The handler never calls the model, Kubernetes or Telegram: any outbound request would have raised.
    assert fakes.router.calls.call_count == 0
    assert fakes.openai.route.call_count == 0


async def test_firing_then_resolved_are_two_rows(client):
    await client.post("/alertmanager", json=load_fixture("am_5xx_firing.json"))
    await client.post("/alertmanager", json=load_fixture("am_5xx_resolved.json"))
    assert [r["status"] for r in await rows(client)] == ["firing", "resolved"]


@pytest.mark.parametrize("body", [b"{not json", b'{"alerts": "x"}', b'{"alerts": [{"status": "weird", "labels": {}}]}', b"[]", b'{"alerts": [{"status": "firing"}]}'])
async def test_malformed_is_400_and_stores_nothing(client, body):
    r = await client.post("/alertmanager", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert await rows(client) == []


async def test_db_down_is_503(client):
    client.app.state.store.close()
    r = await client.post("/alertmanager", json=load_fixture("am_5xx_firing.json"))
    assert r.status_code == 503
    assert (await client.get("/healthz")).status_code == 503


async def test_secrets_in_payload_never_reach_the_database(client):
    secret = "sk-" + "q" * 40
    alert = make_alert(summary_label="x")
    alert["annotations"] = {"summary": f"login failed password=hunter2hunter2 key {secret}", "description": f"bot {FAKE_TG_TOKEN}"}
    alert["labels"]["user"] = "alice.nguyen@example.com"
    await client.post("/alertmanager", json={"alerts": [alert]})
    blob = json.dumps(await rows(client))
    for needle in ("hunter2hunter2", secret, FAKE_TG_TOKEN, "alice.nguyen@example.com", FAKE_OPENAI_KEY):
        assert needle not in blob


def test_demo_mode_needs_no_credentials(tmp_path):
    assert make_settings(tmp_path, demo_mode=True, openai_api_key="", telegram_bot_token="", telegram_chat_id="").missing_credentials() == []
    assert make_settings(tmp_path, openai_api_key="").missing_credentials() == ["OPENAI_API_KEY"]
