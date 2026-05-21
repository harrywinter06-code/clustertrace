"""Large payloads get truncated so they don't blow up the DB."""
import json

import clustertrace
from clustertrace import storage


def test_large_input_truncated(monkeypatch):
    monkeypatch.setenv("CLUSTERTRACE_MAX_PAYLOAD_BYTES", "2000")
    big = "x" * 50_000

    @clustertrace.trace
    def go(payload): return payload

    go(big)
    with storage.connect() as c:
        row = c.execute("SELECT input_json FROM spans").fetchone()
    parsed = json.loads(row["input_json"])
    assert parsed.get("__truncated") is True
    assert parsed.get("original_bytes", 0) > 2000
    assert len(row["input_json"]) <= 2200  # within cap + json wrapping overhead


def test_small_input_not_truncated(monkeypatch):
    monkeypatch.setenv("CLUSTERTRACE_MAX_PAYLOAD_BYTES", "8192")

    @clustertrace.trace
    def go(x): return x

    go({"hello": "world"})
    with storage.connect() as c:
        row = c.execute("SELECT input_json FROM spans").fetchone()
    parsed = json.loads(row["input_json"])
    assert parsed != {"__truncated": True}  # not the truncation marker
    assert "hello" in row["input_json"]
