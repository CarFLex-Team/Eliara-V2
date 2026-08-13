import pytest

from app.execution.executor import ReadOnlyExecutor
from tests.fixtures.fixture_db import build_fixture_db


@pytest.fixture()
def fixture_db(tmp_path):
    return build_fixture_db(tmp_path / "eliara_fixture.db")


@pytest.fixture()
def executor(fixture_db):
    ex = ReadOnlyExecutor(fixture_db, query_timeout_s=2.0, max_rows=500)
    yield ex
    ex.close()
