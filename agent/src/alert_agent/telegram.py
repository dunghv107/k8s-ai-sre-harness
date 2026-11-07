"""Telegram: send only (outbound, redacted). Commands via long polling come with US4."""
import logging

import httpx

from .config import Settings
from .redact import redact

log = logging.getLogger("alert_agent.telegram")
MAX_CHARS = 3500


class TelegramError(Exception):
    pass


def _fit(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    # Cut at a line end so no HTML tag or entity is split; render.py closes every tag on its own line.
    cut = text[:MAX_CHARS].rsplit("\n", 1)[0] if "\n" in text[:MAX_CHARS] else text[:MAX_CHARS]
    return cut + "\n…"


class Telegram:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client or httpx.AsyncClient(timeout=15)

    async def send(self, html: str) -> int | None:
        """Post to the alert group; returns the message id (None in demo mode)."""
        text = _fit(redact(html))
        if self.s.demo_mode:
            log.info("demo mode, telegram message not sent:\n%s", text)
            return None
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.s.telegram_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            r = await self._client.post(url, json=payload)
        except httpx.HTTPError as e:
            # Not str(e): httpx messages can carry the URL, which holds the bot token.
            raise TelegramError(f"telegram request failed: {type(e).__name__}") from None
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not body.get("ok"):
            raise TelegramError(f"telegram http {r.status_code}: {redact(str(body.get('description', '')))}")
        return body["result"]["message_id"]

    async def aclose(self) -> None:
        await self._client.aclose()


async def poll_commands() -> None:
    """Long polling for operator commands."""
    # shortcut: no inbound commands in the MVP, build getUpdates polling in US4 (T034).
    return None
