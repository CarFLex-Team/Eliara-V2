"""Regression: token-count fields were redacted (substring 'token' matched)."""

from app.core.logging import _redact_secrets


def test_observability_fields_survive():
    event = {
        "input_tokens": 2895, "output_tokens": 350, "prompt_tag": "x@v1",
        "latency_ms": 12, "cache_scope": "('view', 'vw_q002')",
    }
    result = _redact_secrets(None, None, dict(event))
    assert result == event  # nothing redacted


def test_real_secrets_redacted():
    event = {
        "api_key": "sk-live", "token": "abc", "anthropic_api_key": "sk-x",
        "db_password": "p", "authorization": "Bearer x", "key": "v",
    }
    result = _redact_secrets(None, None, dict(event))
    assert all(v == "[REDACTED]" for v in result.values())
