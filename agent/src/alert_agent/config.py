"""Settings read from the environment (names and defaults: specs/001-realtime-alert-triage/contracts/configuration.md)."""
import os
from dataclasses import dataclass

# USD per 1M tokens (input, output); unknown models are billed at the dearest known rate.
PRICES = {"gpt-6.1-terra": (1.0, 5.0), "gpt-6.1-sol": (2.0, 10.0)}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, max(PRICES.values()))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _engine(value: str) -> str:
    value = value.strip().lower()
    if value not in ("native", "holmes", "both", "fallback"):
        raise ValueError(f"AGENT_ENGINE must be native, holmes, both or fallback, got {value!r}")
    return value


def _list(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    openai_model: str = "gpt-6.1-terra"
    openai_model_escalate: str = "gpt-6.1-sol"
    openai_base_url: str | None = None
    daily_cost_cap_usd: float = 2.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: tuple[str, ...] = ()
    timezone: str = "Asia/Ho_Chi_Minh"
    batch_window_seconds: int = 60
    max_parallel_subagents: int = 5
    subagent_timeout_seconds: int = 300
    demo_mode: bool = False
    namespaces: tuple[str, ...] = ("shop",)
    gate_pod_age_seconds: int = 180
    gate_min_errors: int = 3
    prometheus_url: str = "http://kps-prometheus.observability:9090"
    loki_url: str = "http://loki.observability:3100"
    tempo_url: str = "http://tempo.observability:3200"
    agent_engine: str = "fallback"  # native | holmes | both | fallback (native, then Holmes once when native is unknown or low)
    holmes_url: str = "http://holmes-holmes.alerting.svc"
    holmes_api_key: str = ""
    # Not in the contract: plumbing and test knobs.
    db_path: str = "/data/agent.db"
    k8s_url: str = "https://kubernetes.default.svc"
    k8s_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    k8s_ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    poll_interval_seconds: float = 2.0
    hold_recheck_seconds: int = 30
    hold_max_seconds: int = 600
    correlation_window_seconds: int = 120
    max_tool_calls: int = 8
    run_workers: bool = True

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        e = os.environ if env is None else env
        d = cls()  # defaults live in one place: the dataclass

        def get(name, default, cast=str):
            raw = e.get(name)
            return default if raw is None or raw == "" else cast(raw)

        return cls(
            openai_api_key=get("OPENAI_API_KEY", d.openai_api_key),
            openai_model=get("OPENAI_MODEL", d.openai_model),
            openai_model_escalate=get("OPENAI_MODEL_ESCALATE", d.openai_model_escalate),
            daily_cost_cap_usd=get("AGENT_DAILY_COST_CAP_USD", d.daily_cost_cap_usd, float),
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN", d.telegram_bot_token),
            telegram_chat_id=get("TELEGRAM_ALERT_CHAT_ID", d.telegram_chat_id),
            telegram_allowed_user_ids=get("TELEGRAM_ALLOWED_USER_IDS", d.telegram_allowed_user_ids, _list),
            timezone=get("AGENT_TIMEZONE", d.timezone),
            batch_window_seconds=get("AGENT_BATCH_WINDOW_SECONDS", d.batch_window_seconds, int),
            max_parallel_subagents=get("AGENT_MAX_PARALLEL_SUBAGENTS", d.max_parallel_subagents, int),
            subagent_timeout_seconds=get("AGENT_SUBAGENT_TIMEOUT_SECONDS", d.subagent_timeout_seconds, int),
            demo_mode=get("AGENT_DEMO_MODE", d.demo_mode, _bool),
            namespaces=get("AGENT_NAMESPACES", d.namespaces, _list),
            gate_pod_age_seconds=get("AGENT_GATE_POD_AGE_SECONDS", d.gate_pod_age_seconds, int),
            gate_min_errors=get("AGENT_GATE_MIN_ERRORS", d.gate_min_errors, int),
            prometheus_url=get("PROMETHEUS_URL", d.prometheus_url),
            loki_url=get("LOKI_URL", d.loki_url),
            tempo_url=get("TEMPO_URL", d.tempo_url),
            agent_engine=_engine(get("AGENT_ENGINE", d.agent_engine)),
            holmes_url=get("HOLMES_URL", d.holmes_url),
            holmes_api_key=get("HOLMES_API_KEY", d.holmes_api_key),
            db_path=get("AGENT_DB_PATH", d.db_path),
        )

    def missing_credentials(self) -> list[str]:
        """Names of required secrets that are empty; demo mode needs none."""
        if self.demo_mode:
            return []
        need = {
            "OPENAI_API_KEY": self.openai_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_ALERT_CHAT_ID": self.telegram_chat_id,
        }
        return [name for name, value in need.items() if not value]
