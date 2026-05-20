"""Test that the schema migration runner upgrades v1 databases to v2 cleanly."""
import sqlite3

import agentlog
from agentlog import storage


def _v1_schema():
    """The original v1 schema as it existed at v0.1.0 — used to simulate an old DB."""
    return """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS traces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        status TEXT NOT NULL DEFAULT 'running',
        error_type TEXT,
        error_message TEXT
    );
    CREATE TABLE IF NOT EXISTS spans (
        id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL,
        parent_id TEXT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        status TEXT NOT NULL DEFAULT 'running',
        error_type TEXT,
        error_message TEXT,
        input_json TEXT,
        output_json TEXT,
        attrs_json TEXT
    );
    INSERT INTO schema_meta(key, value) VALUES('version', '1');
    """


def test_fresh_db_initializes_at_latest_version(isolated_db):
    @agentlog.trace
    def go(): pass
    go()
    with storage.connect() as c:
        v = c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
    assert int(v) == storage._SCHEMA_VERSION


def test_v1_db_gets_migrated_to_v2(tmp_path, monkeypatch):
    """Simulate an old DB and confirm migration adds the v2 columns/tables."""
    db = tmp_path / "v1.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(_v1_schema())
    raw.commit()
    raw.close()

    monkeypatch.setenv("AGENTLOG_DB", str(db))
    storage.reset_initialized_cache()

    # Opening triggers the migration runner.
    with storage.connect() as c:
        v = int(c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0])
        cols = {r[1] for r in c.execute("PRAGMA table_info(traces)").fetchall()}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert v == storage._SCHEMA_VERSION
    assert "signature" in cols
    assert "trace_tags" in tables


def test_migrations_are_idempotent(isolated_db):
    """Re-running the migration runner on a current DB is a no-op."""
    @agentlog.trace
    def go(): pass
    go()

    # Force re-initialization
    storage.reset_initialized_cache()
    with storage.connect() as c:
        v_before = int(c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0])

    storage.reset_initialized_cache()
    with storage.connect() as c:
        v_after = int(c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0])
    assert v_before == v_after == storage._SCHEMA_VERSION
