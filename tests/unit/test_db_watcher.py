import time

from app.execution.db_watcher import DatabaseWatcher
from app.execution.executor import ReadOnlyExecutor
from tests.fixtures.fixture_db import build_fixture_db


def test_no_change_no_trigger(fixture_db):
    watcher = DatabaseWatcher(fixture_db)
    assert watcher.check_once() is False


def test_refresh_detected_and_executor_reopened(tmp_path):
    db = build_fixture_db(tmp_path / "live.db")
    executor = ReadOnlyExecutor(db)
    watcher = DatabaseWatcher(db)
    watcher.on_change(executor.reopen)
    try:
        before = executor.run_view("fact_ai_sales_net").row_count
        assert before == 3

        time.sleep(0.01)
        build_fixture_db(db, extra_sales_rows=5)  # simulate SAP B1 refresh

        assert watcher.check_once() is True
        after = executor.run_view("fact_ai_sales_net").row_count
        assert after == 8
        assert watcher.last_change_detected is not None
    finally:
        executor.close()


def test_callback_failure_does_not_break_watcher(tmp_path):
    db = build_fixture_db(tmp_path / "cb.db")
    watcher = DatabaseWatcher(db)
    watcher.on_change(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    build_fixture_db(db, extra_sales_rows=1)
    assert watcher.check_once() is True  # survives the failing callback
