"""Read-only Prometheus, Loki and Tempo queries. Grafana is not involved."""
import math
import re
import time
from datetime import datetime, timezone

import httpx

from ..config import Settings
from .k8s import ToolError, clip

MAX_POINTS, MAX_SERIES, MAX_LINES, MAX_TRACES, MAX_SPANS = 60, 10, 50, 5, 60
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Control-plane identities that rewrite objects all day; they would bury the human and pipeline changes.
_AUDIT_NOISE = "system:kube-controller-manager|system:kube-scheduler|system:apiserver|system:kube-proxy|system:k3s-.*|system:node:.*|system:serviceaccount:kube-system:.*"
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{16,32}$")


def _minutes(value: int) -> int:
    return max(1, min(int(value), 120))


def _attr(attrs: list[dict], key: str) -> str:
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value", {})
            return str(next(iter(v.values()), "")) if v else ""
    return ""


class Telemetry:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client or httpx.AsyncClient(timeout=20)

    async def _get(self, url: str, **params) -> httpx.Response:
        r = await self._client.get(url, params=params)
        if r.status_code >= 400:
            raise ToolError(f"{url.split('/')[2]} returned {r.status_code}")
        return r

    async def query_metrics(self, promql: str, minutes: int = 30) -> str:
        end = time.time()
        span = _minutes(minutes) * 60
        step = max(15, math.ceil(span / (MAX_POINTS - 1)))  # keeps every series at or under 60 points
        r = await self._get(f"{self.s.prometheus_url}/api/v1/query_range", query=promql, start=end - span, end=end, step=step)
        series = r.json().get("data", {}).get("result", [])
        if not series:
            return "no data"
        lines = []
        for s in series[:MAX_SERIES]:
            vals = s.get("values", [])[-MAX_POINTS:]
            nums = [v[1] for v in vals]
            lines.append(f"{s.get('metric', {})} step={step}s n={len(nums)} values={','.join(nums)}")
        if len(series) > MAX_SERIES:
            lines.append(f"(+{len(series) - MAX_SERIES} more series not shown)")
        return clip("\n".join(lines))

    async def query_logs(self, logql: str, minutes: int = 30, limit: int = 20) -> str:
        end = time.time()
        r = await self._get(
            f"{self.s.loki_url}/loki/api/v1/query_range",
            query=logql,
            start=int((end - _minutes(minutes) * 60) * 1e9),
            end=int(end * 1e9),
            limit=max(1, min(int(limit), MAX_LINES)),
            direction="backward",
        )
        rows = []
        for stream in r.json().get("data", {}).get("result", []):
            rows += [(int(ts), line) for ts, line in stream.get("values", [])]
        rows.sort(reverse=True)  # newest first across streams
        if not rows:
            return "no log lines"
        out = [f"{datetime.fromtimestamp(ts / 1e9, timezone.utc).strftime('%H:%M:%SZ')} {line}" for ts, line in rows[:MAX_LINES]]
        return clip("\n".join(out))

    async def audit_changes(self, namespace: str, minutes: int = 60, limit: int = 20) -> str:
        """Writes in one namespace from the Kubernetes audit stream (Loki job=k8s-audit), newest first; the impersonated identity wins as actor."""
        if not _SAFE.match(namespace):
            raise ToolError("invalid namespace")
        logql = (
            '{job="k8s-audit"} | json verb="verb", ns="objectRef.namespace", resource="objectRef.resource", sub="objectRef.subresource",'
            ' name="objectRef.name", user="user.username", iuser="impersonatedUser.username", code="responseStatus.code"'
            f' | ns="{namespace}" | verb=~"create|update|patch|delete|deletecollection" | resource!~"events|leases|endpoints|endpointslices"'
            f' | sub!~"status|binding" | user!~"{_AUDIT_NOISE}"'
            " | line_format `{{.verb}} {{.resource}} {{.ns}}/{{.name}} by {{if .iuser}}{{.iuser}} (impersonated by {{.user}}){{else}}{{.user}}{{end}} code={{.code}}`"
        )
        return await self.query_logs(logql, minutes, limit)

    async def get_trace(self, trace_id: str) -> str:
        if not _TRACE_ID.match(trace_id):
            raise ToolError("invalid trace_id")
        data = (await self._get(f"{self.s.tempo_url}/api/traces/{trace_id}")).json()
        spans = []
        for batch in data.get("batches") or data.get("resourceSpans") or []:
            service = _attr(batch.get("resource", {}).get("attributes", []), "service.name")
            for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
                for sp in scope.get("spans", []):
                    spans.append((service, sp))
        if not spans:
            return "trace not found or empty"
        by_id = {sp.get("spanId"): sp for _, sp in spans}

        def depth(sp):
            d = 0
            while sp.get("parentSpanId") in by_id and d < 10:
                sp, d = by_id[sp["parentSpanId"]], d + 1
            return d

        lines = []
        for service, sp in sorted(spans, key=lambda x: int(x[1].get("startTimeUnixNano", 0)))[:MAX_SPANS]:
            ms = (int(sp.get("endTimeUnixNano", 0)) - int(sp.get("startTimeUnixNano", 0))) / 1e6
            code = str(sp.get("status", {}).get("code", "UNSET"))
            attrs = sp.get("attributes", [])
            lines.append(
                f"{'  ' * depth(sp)}{service} {sp.get('name', '')} {ms:.1f}ms status={code.replace('STATUS_CODE_', '')}"
                f" route={_attr(attrs, 'http.route') or '-'} msg={sp.get('status', {}).get('message', '') or '-'}"
            )
        return clip("\n".join(lines))

    async def search_traces(self, service: str, minutes: int = 30, status_error: bool = True) -> str:
        if not _SAFE.match(service):  # service goes into a TraceQL string: no quotes allowed
            raise ToolError("invalid service")
        end = int(time.time())
        cond = f'resource.service.name="{service}"' + (" && status=error" if status_error else "")
        r = await self._get(
            f"{self.s.tempo_url}/api/search", q="{" + cond + "}", start=end - _minutes(minutes) * 60, end=end, limit=MAX_TRACES
        )
        ids = [t["traceID"] for t in r.json().get("traces", [])][:MAX_TRACES]
        return clip("\n".join(ids)) if ids else "no traces"

    async def count_errors(self, namespace: str, service: str, seconds: int = 120) -> int | None:
        """Error log lines of one service in the last `seconds`; None when Loki cannot answer (gate helper, not a tool)."""
        if not (_SAFE.match(namespace) and _SAFE.match(service)):
            return None
        q = f'sum(count_over_time({{namespace="{namespace}", app="{service}", level="error"}}[{int(seconds)}s]))'
        try:
            r = await self._get(f"{self.s.loki_url}/loki/api/v1/query", query=q)
            result = r.json().get("data", {}).get("result", [])
            return int(float(result[0]["value"][1])) if result else 0
        except (httpx.HTTPError, ToolError, KeyError, ValueError, IndexError):
            return None

    async def aclose(self) -> None:
        await self._client.aclose()
