"""Per-test SQLite isolation: each test gets a fresh tmp DB via AGENTLOG_DB."""
import pytest

from agentlog import storage


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "traces.db"
    monkeypatch.setenv("AGENTLOG_DB", str(db_file))
    storage.reset_initialized_cache()
    yield db_file
    storage.reset_initialized_cache()
