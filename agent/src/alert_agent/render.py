"""Vietnamese Telegram messages (contracts/diagnosis-output.md). HTML-escaped; every tag is closed on its own line."""
from html import escape
from zoneinfo import ZoneInfo

from .store import parse_iso

MAX_EVIDENCE_LINES = 3
MAX_HUMAN_CHECKS = 3
PROTECTED_LINE = "🔒 Bảo mật — không tự đóng, cần người xác nhận"


def _local(ts: str, tz: str) -> str:
    return parse_iso(ts).astimezone(ZoneInfo(tz)).strftime("%d/%m %H:%M")


def _uniq(values) -> str:
    return ", ".join(dict.fromkeys(v for v in values if v))


def _title(incidents: list[dict]) -> str:
    ids = " ".join(f"#{i['id']}" for i in incidents)
    return f"{ids} · {escape(_uniq(i['service'] for i in incidents))} · {escape(_uniq(i['rule'] for i in incidents))}"


def render_diagnosis(incidents: list[dict], verdict: dict, model: str, cost: float, tz: str, engine: str | None = None, seconds: float | None = None) -> str:
    """One message for one run; correlated incidents share it. `engine` and `seconds` are set when two engines are involved (AGENT_ENGINE=both or fallback)."""
    first_seen = min(i["first_seen"] for i in incidents)
    tag = f"[{engine}] " if engine else ""
    lines = [
        f"🧠 <b>{tag}Chẩn đoán {_title(incidents)}</b>",
        f"🏷️ {escape(verdict['label'])} · {escape(verdict['status'])} · tin cậy {escape(verdict['confidence'])}",
        f"🎯 {escape(verdict['summary'])}",
    ]
    if verdict.get("cause"):
        lines.append(f"🔎 {escape(verdict['cause'])}")
    for e in verdict["evidence"][:MAX_EVIDENCE_LINES]:
        lines.append(f"📎 {escape(e['source'])}: {escape(e['excerpt'])}")
    lines.append(f"➡️ {escape(verdict['next_step'])}")
    checks = verdict.get("human_checks") or []
    if checks:
        lines.append("🧑‍💻 Chỉ người làm được:")
        lines += [f"• {escape(c)}" for c in checks[:MAX_HUMAN_CHECKS]]
    if any(i["protected"] for i in incidents):
        lines.append(PROTECTED_LINE)
    occurrences = sum(i["occurrences"] for i in incidents)
    took = f" · {seconds:.0f}s" if seconds is not None else ""
    lines.append(f"⏱️ {_local(first_seen, tz)} · {occurrences} lần · {escape(model)}{took} · {cost:.4f} USD")
    return "\n".join(lines)


def render_out_of_scope(incidents: list[dict], namespaces: tuple[str, ...]) -> str:
    lines = [
        f"⚠️ <b>Sự cố {_title(incidents)}</b>",
        f"🚧 Chưa chẩn đoán: namespace {escape(_uniq(i['namespace'] for i in incidents))} nằm ngoài phạm vi agent được đọc ({escape(', '.join(namespaces))}).",
    ]
    if any(i["protected"] for i in incidents):
        lines.append(PROTECTED_LINE)
    return "\n".join(lines)


def render_cost_cap(incidents: list[dict], spent: float, cap: float) -> str:
    lines = [
        f"⚠️ <b>Sự cố {_title(incidents)}</b>",
        f"💸 Chưa chẩn đoán: đã chạm trần chi phí hôm nay ({spent:.4f}/{cap:g} USD).",
    ]
    if any(i["protected"] for i in incidents):
        lines.append(PROTECTED_LINE)
    return "\n".join(lines)


def render_run_cap(incidents: list[dict], runs: int, cap: int) -> str:
    lines = [
        f"⚠️ <b>Sự cố {_title(incidents)}</b>",
        f"🚦 Chưa chẩn đoán: đã chạm trần số lần chạy mô hình hôm nay ({runs}/{cap}).",
    ]
    if any(i["protected"] for i in incidents):
        lines.append(PROTECTED_LINE)
    return "\n".join(lines)
