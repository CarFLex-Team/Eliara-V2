from pathlib import Path

from app.core.config import Settings


def test_defaults_are_safe():
    s = Settings(_env_file=None)
    assert s.history_size == 5
    assert s.max_rows == 500
    assert s.query_timeout_s == 30
    assert s.orchestrator_model.startswith("claude-sonnet")
    assert s.sqlgen_model.startswith("claude-haiku")


def test_env_override(monkeypatch):
    monkeypatch.setenv("ELIARA_MAX_ROWS", "100")
    monkeypatch.setenv("ELIARA_DB_PATH", "/tmp/x.db")
    s = Settings(_env_file=None)
    assert s.max_rows == 100
    assert s.db_path == Path("/tmp/x.db")


def test_api_key_is_secret():
    s = Settings(_env_file=None, anthropic_api_key="sk-test-123")
    assert "sk-test-123" not in repr(s)
