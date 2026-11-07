import pytest

from alert_agent.redact import redact

# Fake values assembled from pieces so secret scanners do not flag this file.
SEEDED = {
    "bearer": ("Authorization: Bearer " + "abcdefghij1234567890XYZ", "abcdefghij1234567890XYZ"),
    "basic": ("authorization: Basic " + "dXNlcjpodW50ZXIy", "dXNlcjpodW50ZXIy"),
    "password": ("db password=" + "hunter2hunter2 failed", "hunter2hunter2"),
    "token-json": ('{"api_token": "' + "s3cr3t-value-123" + '"}', "s3cr3t-value-123"),
    "jwt": ("jwt " + "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N", "eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
    "telegram": ("bot " + "123456789:" + "A" * 35, "A" * 35),
    "openai": ("key " + "sk-" + "proj-" + "x" * 30, "x" * 30),
    "aws": ("aws " + "AKIA" + "ABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    "pem": ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAfakefakefake\n-----END RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAfakefakefake"),
    "url": ("connect postgres://shop:" + "pa55w0rdvalue" + "@postgres:5432/shop", "pa55w0rdvalue"),
    "email": ("user alice.nguyen@example.com logged in", "alice.nguyen@example.com"),
    "phone-intl": ("call +84 912 345 678 now", "912 345 678"),
    "phone-vn": ("sdt 0912345678 ok", "0912345678"),
    "cookie": ("Cookie: session=" + "abc123def456ghi789", "abc123def456ghi789"),
}


@pytest.mark.parametrize("kind", SEEDED)
def test_seeded_secret_disappears(kind):
    text, secret = SEEDED[kind]
    out = redact(text)
    assert secret not in out
    assert "«redacted:" in out


def test_all_kinds_in_one_blob():
    out = redact("\n".join(t for t, _ in SEEDED.values()))
    assert all(secret not in out for _, secret in SEEDED.values())


NORMAL_LINES = [
    '{"ts":"2026-11-07T01:12:31Z","level":"info","service":"orders-api","msg":"POST /api/orders 201","route":"/api/orders","status":201,"duration_ms":12,"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}',
    "TypeError: 'NoneType' object is not subscriptable",
    "Readiness probe failed: HTTP probe failed with statuscode: 503",
    "token_count=42 tokens used, basic validation passed",
    "pod catalog-api-7d9f8c6b5-abcde restarted 3 times in 10m at 10.42.0.17",
    "GET /api/products/4 500 1699999999999",
]


@pytest.mark.parametrize("line", NORMAL_LINES)
def test_normal_lines_unchanged(line):
    assert redact(line) == line


def test_idempotent():
    once = redact("password=hunter2hunter2")
    assert redact(once) == once
