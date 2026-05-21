"""Per-test SQLite isolation: each test gets a fresh tmp DB via CLUSTERTRACE_DB."""
import pytest

from clustertrace import storage


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "traces.db"
    monkeypatch.setenv("CLUSTERTRACE_DB", str(db_file))
    storage.reset_initialized_cache()
    yield db_file
    storage.reset_initialized_cache()
