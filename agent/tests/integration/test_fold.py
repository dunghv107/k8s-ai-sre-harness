"""Late duplicate alert groups fold into the group that already runs or ran; unrelated incidents keep their own run."""
import json

from conftest import make_alert, verdict_json


def a(name="Http5xxRate", svc="catalog-api", **kw):
    return make_alert(alertname=name, namespace="shop", service=svc, http_route="/r", **kw)


def audit(name, user="demo-operator", rule="AuditNetworkPolicyChange"):
    return make_alert(alertname=rule, source="loki-audit", severity="warning", protected="true", namespace="shop", user=user, name=name, service="k8s-audit")


async def test_late_group_for_the_same_service_folds_into_the_finished_diagnosis(make_harness, fakes):
    h = make_harness()
    await h.post(a())
    await h.settle()
    await h.post(a(name="AppErrorLogs", svc="catalog-api", severity="warning"))
    await h.settle()
    first, second = await h.incidents()
    assert len(await h.runs()) == 1 and len(fakes.telegram_bodies) == 1
    assert second["state"] == "diagnosed" and second["verdict"] == first["verdict"] and "gộp vào chẩn đoán #1" in second["gate_reason"]


async def test_group_arriving_while_the_first_runs_waits_then_folds(make_harness, fakes):
    h = make_harness()
    fakes.openai.delay = 0.2
    await h.post(a())
    await h.orch.tick()  # first run starts and is still analysing
    await h.post(a(name="AppErrorLogs", svc="shop-web", severity="warning"))  # shop-web is named in the verdict below
    fakes.openai.final = lambda body: verdict_json(summary="shop-web trả 502 vì catalog-api lỗi.")
    await h.orch.tick()
    assert (await h.incidents("folded"))[0]["id"] == 2
    await h.settle()
    assert len(await h.runs()) == 1 and len(fakes.telegram_bodies) == 1
    assert [i["state"] for i in await h.incidents()] == ["diagnosed", "diagnosed"]


async def test_unrelated_service_gets_its_own_run(make_harness, fakes):
    h = make_harness()
    await h.post(a())
    await h.settle()
    await h.post(a(name="PodRestartingFrequently", svc="invoice-api", severity="warning"))
    await h.settle()
    states = [i["state"] for i in await h.incidents()]
    assert states == ["diagnosed", "diagnosed"] and len(await h.runs()) == 2 and len(fakes.telegram_bodies) == 2
    assert "chạy riêng" in (await h.incidents())[1]["gate_reason"]


async def test_protected_duplicates_of_same_rule_and_actor_share_one_run(make_harness, fakes):
    h = make_harness()
    await h.post(audit("backends-egress-postgres"), audit("postgres-ingress"))
    await h.settle()
    assert len(await h.runs()) == 1 and len(fakes.telegram_bodies) == 1
    # a second policy edit by the same actor after the run folds in; another actor does not
    await h.post(audit("another-policy"), audit("other", user="someone-else"))
    await h.settle()
    assert len(await h.runs()) == 2
    states = {i["id"]: i for i in await h.incidents()}
    assert "gộp vào" in states[3]["gate_reason"] and json.loads(states[3]["verdict"])["label"] == "app"


async def test_resolve_closes_a_folded_incident(make_harness, fakes):
    h = make_harness()
    fakes.openai.delay = 0.2
    await h.post(a())
    await h.orch.tick()
    late = a(name="AppErrorLogs", svc="catalog-api", severity="warning")
    await h.post(late)
    await h.orch.tick()
    assert (await h.incidents("folded"))
    await h.post({**late, "status": "resolved"})
    await h.orch.tick()
    assert (await h.incidents("resolved"))[0]["id"] == 2
    await h.settle()
