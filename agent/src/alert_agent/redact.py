"""Mask secrets and personal data; runs on every tool result, stored text and outgoing message."""
import re


def _tag(kind: str) -> str:
    return f"«redacted:{kind}»"


# (kind, pattern, replacement). Order matters: specific shapes first, generic key=value pairs after.
_RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), _tag("private-key")),
    ("url-credentials", re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1" + _tag("url-credentials") + "@"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), _tag("jwt")),
    ("bearer", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 " + _tag("bearer")),
    ("basic", re.compile(r"(?i)(authorization\s*[:=]\s*)basic\s+[A-Za-z0-9+/=]{8,}"), r"\1Basic " + _tag("basic")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), _tag("openai-key")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _tag("aws-key")),
    ("telegram-token", re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{30,}"), _tag("telegram-token")),
    ("cookie", re.compile(r"(?i)\b(set-cookie|cookie)(\s*:\s*)[^\r\n]+"), r"\1\2" + _tag("cookie")),
    (
        "secret",
        re.compile(
            r"(?i)([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key))"
            r"([\"']?\s*[:=]\s*[\"']?)(?!«redacted)([^\s\"',;&]+)"
        ),
        r"\1\2" + _tag("secret"),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), _tag("email")),
    # International numbers need a leading +, national ones the Vietnamese 0 prefix: plain digit runs (ids, timestamps) stay.
    ("phone", re.compile(r"(?<![\w.])(?:\+\d[\d ().-]{7,}\d|0\d{9,10})(?![\w.])"), _tag("phone")),
]


def redact(text: str) -> str:
    for _kind, pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
