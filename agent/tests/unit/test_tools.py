import json
import re
from pathlib import Path

import httpx
import pytest

import alert_agent.tools.k8s as k8s_module
from alert_agent.tools import TOOL_SPECS, Toolbox
from alert_agent.tools.k8s import MAX_OUTPUT, K8s, ToolError
from alert_agent.tools.telemetry import Telemetry
from conftest import CATALOG_POD, ERROR_LINE, make_settings

# Fake secret assembled from pieces so scanners stay quiet.
SECRET = "sk-" + "z" * 40


@pytest.fixture
def tools(tmp_path, fakes):
    s = make_settings(tmp_path)
    return K8s(s), Telemetry(s)


async def test_list_pods_selector_and_fields(tools, fakes):
    k8s, _ = tools
    out = await k8s.list_pods("shop", "app.kubernetes.io/name=catalog-api")
    assert fakes.router.calls.last.request.url.params["labelSelector"] == "app.kubernetes.io/name=catalog-api"
    assert CATALOG_POD in out and "ready=True" in out and "restarts=0" in out


async def test_pod_logs_url_params_grep_and_clamp(tools, fakes):
    k8s, _ = tools
    fakes.log_text = "ok line\n" + ERROR_LINE + "\nanother ok"
    out = await k8s.get_pod_logs("shop", CATALOG_POD, since_minutes=500, previous=True, grep="TYPEERROR")
    params = fakes.router.calls.last.request.url.params
    assert params["sinceSeconds"] == "3600" and params["tailLines"] == "400" and params["previous"] == "true"
    assert "TypeError" in out and "ok line" not in out


async def test_logs_truncated_and_redacted(tools, fakes):
    k8s, _ = tools
    fakes.log_text = ("x" * 100 + "\n") * 200 + f"token={SECRET}\n"
    out = await k8s.get_pod_logs("shop", CATALOG_POD)
    assert len(out) <= MAX_OUTPUT + 20
    assert SECRET not in out and "«redacted:" in out


async def test_events_field_selector(tools, fakes):
    k8s, _ = tools
    out = await k8s.get_events("shop", CATALOG_POD)
    assert fakes.router.calls.last.request.url.params["fieldSelector"] == f"involvedObject.name={CATALOG_POD}"
    assert "Readiness probe failed" in out


async def test_rollout_summary(tools, fakes):
    k8s, _ = tools
    fakes.rolling = True
    out = await k8s.get_rollout("shop", "catalog-api")
    assert "rolling=True" in out and "revision=7" in out and "catalog-api-7d9f8c6b5" in out


@pytest.mark.parametrize("call", ["list_pods", "get_pod_logs", "get_events", "get_rollout"])
async def test_namespace_outside_allow_list_refused(tools, fakes, call):
    k8s, _ = tools
    args = {"list_pods": ("kube-system", "a=b"), "get_pod_logs": ("kube-system", "p"), "get_events": ("kube-system", "p"), "get_rollout": ("kube-system", "d")}[call]
    with pytest.raises(ToolError, match="not allowed"):
        await getattr(k8s, call)(*args)
    assert fakes.router.calls.call_count == 0


async def test_toolbox_turns_refusal_into_text(tools, fakes):
    k8s, telemetry = tools
    out = await Toolbox(k8s, telemetry).call("list_pods", {"namespace": "kube-system", "selector": "a=b"})
    assert out.startswith("error:") and "not allowed" in out


async def test_k8s_only_issues_get(tools, fakes):
    k8s, _ = tools
    catch_all = fakes.router.route(host="k8s.test").mock(return_value=httpx.Response(405))  # reached only by non-GET or unknown paths
    await k8s.list_pods("shop", "a=b")
    await k8s.get_pod_logs("shop", CATALOG_POD)
    await k8s.get_events("shop", CATALOG_POD)
    await k8s.get_rollout("shop", "catalog-api")
    assert catch_all.call_count == 0
    methods = {c.request.method for c in fakes.router.calls if c.request.url.host == "k8s.test"}
    assert methods == {"GET"}


def test_k8s_source_has_no_write_verbs():
    src = Path(k8s_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"client\.(post|put|patch|delete|request|stream|send)\(", src)
    assert "client.get(" in src


async def test_query_metrics_url_points_and_series(tools, fakes):
    _, tel = tools
    series = [{"metric": {"pod": f"p{i}"}, "values": [[1000 + j, str(j)] for j in range(100)]} for i in range(12)]
    route = fakes.router.get("http://kps-prometheus.observability:9090/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"data": {"result": series}})
    )
    out = await tel.query_metrics("up", minutes=500)
    params = route.calls.last.request.url.params
    assert params["query"] == "up"
    assert round(float(params["end"]) - float(params["start"])) == 120 * 60  # minutes clamped to 120
    assert out.count("step=") == 10 and "+2 more series" in out
    assert max(len(line.split("values=")[1].split(",")) for line in out.splitlines() if "values=" in line) <= 60
    assert len(out) <= MAX_OUTPUT + 20


async def test_query_logs_newest_first_limit_and_redaction(tools, fakes):
    _, tel = tools
    values = [[str(1_700_000_000_000_000_000 + i * 1_000_000_000), f"line {i} password=hunter2hunter2"] for i in range(80)]
    route = fakes.router.get("http://loki.observability:3100/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"data": {"result": [{"stream": {}, "values": values}]}})
    )
    out = await tel.query_logs('{app="catalog-api"}', minutes=30, limit=500)
    assert route.calls.last.request.url.params["limit"] == "50"
    lines = out.splitlines()
    assert len(lines) == 50 and "line 79" in lines[0]
    assert "hunter2hunter2" not in out


async def test_get_trace_summary(tools, fakes):
    _, tel = tools
    trace = {"batches": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "catalog-api"}}]},
        "scopeSpans": [{"spans": [
            {"spanId": "a", "name": "GET /api/products/{id}", "startTimeUnixNano": "1000000000", "endTimeUnixNano": "1050000000",
             "status": {"code": "STATUS_CODE_ERROR", "message": "TypeError"}, "attributes": [{"key": "http.route", "value": {"stringValue": "/api/products/{id}"}}]},
            {"spanId": "b", "parentSpanId": "a", "name": "SELECT", "startTimeUnixNano": "1010000000", "endTimeUnixNano": "1020000000", "status": {}},
        ]}],
    }]}
    route = fakes.router.get("http://tempo.observability:3200/api/traces/4bf92f3577b34da6a3ce929d0e0e4736").mock(
        return_value=httpx.Response(200, json=trace)
    )
    out = await tel.get_trace("4bf92f3577b34da6a3ce929d0e0e4736")
    assert route.called
    lines = out.splitlines()
    assert lines[0] == "catalog-api GET /api/products/{id} 50.0ms status=ERROR route=/api/products/{id} msg=TypeError"
    assert lines[1].startswith("  catalog-api SELECT")
    with pytest.raises(ToolError):
        await tel.get_trace("../../etc")


async def test_search_traces_traceql_and_limit(tools, fakes):
    _, tel = tools
    route = fakes.router.get("http://tempo.observability:3200/api/search").mock(
        return_value=httpx.Response(200, json={"traces": [{"traceID": f"{i:032x}"} for i in range(9)]})
    )
    out = await tel.search_traces("catalog-api", 30, True)
    params = route.calls.last.request.url.params
    assert params["q"] == '{resource.service.name="catalog-api" && status=error}'
    assert len(out.splitlines()) == 5
    with pytest.raises(ToolError):
        await tel.search_traces('x"} || {true', 30, True)


async def test_metric_output_redacted(tools, fakes):
    _, tel = tools
    fakes.router.get("http://kps-prometheus.observability:9090/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"data": {"result": [{"metric": {"note": f"token={SECRET}"}, "values": [[1, "1"]]}]}})
    )
    assert SECRET not in await tel.query_metrics("up", 5)


def test_tool_specs_are_strict():
    names = {t["name"] for t in TOOL_SPECS}
    assert names == {"list_pods", "get_pod_logs", "get_events", "get_rollout", "list_network_policies", "query_metrics", "query_logs", "get_trace", "search_traces"}
    for t in TOOL_SPECS:
        p = t["parameters"]
        assert t["strict"] and p["additionalProperties"] is False and set(p["required"]) == set(p["properties"])
    json.dumps(TOOL_SPECS)
