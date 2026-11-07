"""Deterministic gate: drops transient failures before any model call (FR-029..031). No model here."""
import logging
from dataclasses import dataclass
from datetime import datetime

from .config import Settings
from .pack import workload_name
from .store import Store
from .tools.k8s import ToolError
from .tools.k8s import K8s
from .tools.telemetry import Telemetry

log = logging.getLogger("alert_agent.gate")
# Rule (d) only makes sense for alerts that are about error volume.
ERROR_RATE_RULES = ("Http5xxRate", "AppErrorLogs")
WINDOW_SECONDS = 120


@dataclass
class Decision:
    action: str  # queue | hold | self_resolved
    reason: str
    kind: str = ""  # for holds: rollout | young_pod | below_threshold


async def evaluate(inc: dict, *, settings: Settings, store: Store, k8s: K8s, telemetry: Telemetry, now: datetime) -> Decision:
    if inc["protected"]:
        return Decision("queue", "bảo vệ: không qua cửa lọc")
    if await store.latest_alert_status(inc["id"]) == "resolved":
        return Decision("self_resolved", "đã hết trước khi tới lượt phân tích")
    ns, svc = inc["namespace"], inc["service"]
    if not (ns and svc):
        return Decision("queue", "không đủ nhãn để kiểm")

    if await store.marker_active(ns, svc):
        return Decision("hold", f"rollout {svc} đang chạy (ServiceRollingOut)", "rollout")

    note = ""
    if k8s.allowed(ns):
        dep = workload_name(svc)  # a pod name (PodOOMKilled) maps to its Deployment
        try:
            rollout = await k8s.rollout(ns, dep)
            if rollout["rolling"]:
                return Decision("hold", f"rollout {dep} rev {rollout['revision']} đang chạy", "rollout")
            pods = await k8s.pods(ns, f"app.kubernetes.io/name={dep}", now)
        except ToolError as e:  # no such Deployment: the label is not a workload (network, k8s-audit)
            note = f"; không kiểm rollout: '{dep}' không phải Deployment ({str(e)[:60]})"
            pods = []
        except Exception as e:  # an unreachable API must not stop a diagnosis
            log.warning("gate cannot read cluster state for %s/%s: %s", ns, dep, type(e).__name__)
            note = f"; không đọc được trạng thái cụm ({type(e).__name__}), cho qua"
            pods = []
        failing = [p for p in pods if not p["ready"] or p["phase"] != "Running" or p["restarts"] > 0]
        if failing and all(p["age_s"] < settings.gate_pod_age_seconds for p in failing):
            youngest = min(failing, key=lambda p: p["age_s"])
            return Decision("hold", f"Pod {youngest['name']} {youngest['age_s']} giây tuổi", "young_pod")

    if inc["rule"] in ERROR_RATE_RULES:
        errors = await telemetry.count_errors(ns, svc, WINDOW_SECONDS)
        if errors is not None and errors < settings.gate_min_errors:
            return Decision("hold", f"dưới ngưỡng: {errors} lỗi trong {WINDOW_SECONDS} giây", "below_threshold")
    return Decision("queue", "qua cửa lọc" + note)
