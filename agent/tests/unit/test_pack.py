"""Context pack: contents, caps, redaction, gaps, and the NetworkPolicy tool."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from alert_agent.pack import MAX_PACK, build_pack
from alert_agent.tools import Toolbox
from alert_agent.tools.k8s import K8s
from alert_agent.tools.telemetry import Telemetry
from conftest import CATALOG_POD, make_settings

NOW = datetime(2026, 11, 7, 9, 30, 0, tzinfo=timezone.utc)
SECRET = "sk-" + "p" * 40  # fake, assembled so scanners stay quiet


def ts(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def oom_pod(name=CATALOG_POD):
    return {
        "metadata": {"name": name, "creationTimestamp": ts(60), "labels": {"app.kubernetes.io/name": "catalog-api"}},
        "spec": {"containers": [{"name": "catalog-api", "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "160Mi"}}}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "catalog-api", "ready": True, "restartCount": 1, "image": "harbor.local/homelab/catalog-api:0.1.0",
            "state": {"running": {"startedAt": ts(2)}},
            "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": ts(2)}},
        }]},
    }


def healthy_pod(name, app):
    return {"metadata": {"name": name, "creationTimestamp": ts(600), "labels": {"app.kubernetes.io/name": app}}, "spec": {"containers": []},
            "status": {"phase": "Running", "containerStatuses": [{"name": app, "ready": True, "restartCount": 0, "state": {"running": {}}, "lastState": {}}]}}


NETPOL = {
    "metadata": {"name": "postgres-ingress", "creationTimestamp": ts(5000),
                 "managedFields": [{"manager": "kubectl-client-side-apply", "time": ts(4000)}, {"manager": "kubectl-patch", "time": ts(3)}]},
    "spec": {"podSelector": {"matchLabels": {"app": "postgres"}}, "policyTypes": ["Ingress"],
             "ingress": [{"from": [{"podSelector": {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["catalog-api", "invoice-api"]}]}}],
                          "ports": [{"port": 5432, "protocol": "TCP"}]}]},
}
AUDIT = "patch networkpolicies shop/postgres-ingress by demo-operator (impersonated by system:admin) code=200"


def incident(**over):
    base = {"id": 1, "rule": "PodOOMKilled", "source": "prometheus", "severity": "warning", "protected": 0, "namespace": "shop",
            "service": CATALOG_POD, "route": "", "first_seen": ts(1), "last_seen": ts(1), "occurrences": 1}
    return {**base, **over}


@pytest.fixture
def tools(tmp_path, fakes):
    s = make_settings(tmp_path)
    return K8s(s), Telemetry(s)


async def test_pack_has_pod_state_events_policies_audit_and_previous_logs(tools, fakes):
    k8s, tel = tools
    fakes.pods_override = [oom_pod(), healthy_pod("orders-api-6c7d8e9f0-aaaaa", "orders-api")]
    fakes.events = [{"lastTimestamp": ts(2), "type": "Warning", "reason": "BackOff", "message": "Back-off restarting", "count": 3,
                     "involvedObject": {"name": CATALOG_POD}}, {"lastTimestamp": ts(120), "type": "Warning", "reason": "Old", "message": "too old", "count": 1}]
    fakes.netpols = [NETPOL]
    fakes.audit_lines = [AUDIT]
    fakes.log_text = "INFO request\nPOST /fault/oom allocating"
    pack = await build_pack([incident()], k8s, tel, NOW)

    assert "OOMKilled exit=137" in pack and "limits[cpu=- mem=160Mi]" in pack and "(incident workload)" in pack
    assert "1 other pods healthy" in pack
    assert "BackOff" in pack and "too old" not in pack
    assert "postgres-ingress" in pack and "last_modified=" in pack and "kubectl-patch" in pack and "catalog-api,invoice-api" in pack
    assert "demo-operator" in pack and "[audit]" in pack
    assert "POST /fault/oom allocating" in pack  # previous-container tail of the killed pod
    assert "get_rollout" not in pack and "catalog-api: generation=" in pack
    assert len(pack) <= MAX_PACK


async def test_pack_audit_query_uses_the_real_stream_and_namespace(tools, fakes):
    k8s, tel = tools
    fakes.pods_override = [oom_pod()]
    await build_pack([incident()], k8s, tel, NOW)
    q = [c.request.url.params["query"] for c in fakes.router.calls if c.request.url.path.endswith("query_range")][0]
    assert q.startswith('{job="k8s-audit"}') and 'ns="shop"' in q and "impersonatedUser.username" in q
    assert 'verb=~"create|update|patch|delete|deletecollection"' in q


async def test_pack_is_redacted_and_capped(tools, fakes):
    k8s, tel = tools
    fakes.pods_override = [oom_pod()]
    fakes.log_text = f"token={SECRET}\n" + "x" * 5000
    fakes.audit_lines = [f"patch configmaps shop/c by u token={SECRET}"] * 15
    pack = await build_pack([incident()], k8s, tel, NOW)
    assert SECRET not in pack and "«redacted:" in pack and len(pack) <= MAX_PACK


async def test_pack_marks_gaps_instead_of_failing(tools, fakes):
    k8s, tel = tools
    fakes.router.get("http://k8s.test/apis/networking.k8s.io/v1/namespaces/shop/networkpolicies").mock(return_value=httpx.Response(403))
    fakes.router.get("http://loki.observability:3100/loki/api/v1/query_range").mock(return_value=httpx.Response(500))
    fakes.pods_override = [oom_pod()]
    pack = await build_pack([incident()], k8s, tel, NOW)
    assert "unavailable: network policies" in pack and "unavailable: audit" in pack and "OOMKilled" in pack


async def test_pack_is_empty_outside_the_allowed_namespaces(tools, fakes):
    k8s, tel = tools
    assert await build_pack([incident(namespace="observability")], k8s, tel, NOW) == ""
    assert await build_pack([incident(namespace="")], k8s, tel, NOW) == ""


async def test_pack_without_workload_shows_unhealthy_pods(tools, fakes):
    k8s, tel = tools  # HubblePolicyDrops: service is "network", no pod matches
    fakes.pods_override = [oom_pod(), healthy_pod("shop-web-6c7d8e9f0-bbbbb", "shop-web")]
    pack = await build_pack([incident(rule="HubblePolicyDrops", service="network")], k8s, tel, NOW)
    assert CATALOG_POD in pack and "(incident workload)" not in pack and "shop-web" not in pack


async def test_list_network_policies_tool(tools, fakes):
    k8s, tel = tools
    fakes.netpols = [NETPOL, {"metadata": {"name": "deny-all"}, "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}]
    out = await Toolbox(k8s, tel).call("list_network_policies", {"namespace": "shop"})
    assert "postgres-ingress" in out and "selects pods {app=postgres}" in out and "ports 5432/TCP" in out
    assert "deny-all" in out and "ingress from: deny all" in out and "egress to: deny all" in out
    assert fakes.router.calls.last.request.method == "GET"
    assert (await Toolbox(k8s, tel).call("list_network_policies", {"namespace": "kube-system"})).startswith("error:")


async def test_pack_size_on_a_realistic_incident(tools, fakes):
    k8s, tel = tools
    fakes.pods_override = [oom_pod()] + [healthy_pod(f"svc{i}-6c7d8e9f0-aaaaa", f"svc{i}") for i in range(8)]
    fakes.events = [{"lastTimestamp": ts(i), "type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container " + "x" * 80, "count": 3,
                     "involvedObject": {"name": CATALOG_POD}} for i in range(1, 20)]
    fakes.netpols = [NETPOL] * 6
    fakes.audit_lines = [AUDIT] * 15
    fakes.log_text = "\n".join(f"2026-11-07T09:28:{i:02d}Z INFO GET /api/products/{i} 200 4ms" for i in range(40))
    pack = await build_pack([incident()], k8s, tel, NOW)
    print(f"PACK_SIZE chars={len(pack)} approx_tokens={len(pack) // 4}")
    assert len(pack) < 7000
