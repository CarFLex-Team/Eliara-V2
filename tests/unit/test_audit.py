import json

from app.core.audit import AuditTrail


def _record(trail, **overrides):
    payload = dict(
        company_id="beta",
        session_id="s1", question="who are the top customers?",
        decision="use_view", view_used="vw_q002", generated_sql=None,
        cache_hit=False, latency_ms=100, input_tokens=10, output_tokens=5,
        answer="Beta Motors leads.",
    )
    payload.update(overrides)
    trail.record(**payload)


def test_writes_daily_jsonl_with_full_record(tmp_path):
    trail = AuditTrail(tmp_path)
    _record(trail)
    _record(trail, decision="needs_sql", generated_sql="SELECT 1 FROM t", view_used=None)

    files = list(tmp_path.rglob("audit-*.jsonl"))
    assert len(files) == 1
    assert files[0].parent.name == "beta"
    lines = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["question"] == "who are the top customers?"
    assert lines[0]["answer"] == "Beta Motors leads."
    assert lines[0]["company_id"] == "beta"
    assert lines[1]["generated_sql"] == "SELECT 1 FROM t"
    assert "ts" in lines[0] and "session_id" in lines[0]


def test_company_isolated_into_separate_subdirectories(tmp_path):
    trail = AuditTrail(tmp_path)
    _record(trail, company_id="beta")
    _record(trail, company_id="tire_guru")

    beta_files = list((tmp_path / "beta").glob("audit-*.jsonl"))
    tire_guru_files = list((tmp_path / "tire_guru").glob("audit-*.jsonl"))
    assert len(beta_files) == 1
    assert len(tire_guru_files) == 1


def test_disabled_writes_nothing(tmp_path):
    trail = AuditTrail(tmp_path / "off", enabled=False)
    _record(trail)
    assert not (tmp_path / "off").exists()


def test_write_failure_never_raises(tmp_path):
    trail = AuditTrail(tmp_path)
    trail._dir = tmp_path / "does" / "not" / "exist"  # force open() failure
    _record(trail)  # must not raise
