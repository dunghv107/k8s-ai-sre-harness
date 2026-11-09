import pytest

from alert_agent.config import Settings, cost_usd


def test_self_hosted_settings_from_env():
    s = Settings.from_env({"OPENAI_BASE_URL": "http://vllm.ai:8000/v1", "LLM_API": "chat", "OPENAI_MODEL": "qwen3",
                           "LLM_PRICES": "qwen3=0/0, big=0.5/1", "AGENT_DAILY_RUN_CAP": "200",
                           "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALERT_CHAT_ID": "-1"})
    assert s.openai_base_url == "http://vllm.ai:8000/v1" and s.llm_api == "chat" and s.daily_run_cap == 200
    assert s.prices == {"qwen3": (0.0, 0.0), "big": (0.5, 1.0)}
    assert s.missing_credentials() == []  # no OPENAI_API_KEY needed with a base URL


def test_openai_still_needs_a_key():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALERT_CHAT_ID": "-1"})
    assert s.missing_credentials() == ["OPENAI_API_KEY"] and s.llm_api == "responses"


def test_unknown_model_is_billed_at_dearest_rate_unless_priced():
    assert cost_usd("qwen3", 1_000_000, 0) == 2.0
    assert cost_usd("qwen3", 1_000_000, 0, {"qwen3": (0.0, 0.0)}) == 0.0


@pytest.mark.parametrize("env", [{"LLM_API": "grpc"}, {"LLM_PRICES": "qwen3=free"}])
def test_bad_values_fail_at_start(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)
