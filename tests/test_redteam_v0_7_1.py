"""Regression tests for bugs found during the v0.7.1 red-team pass.

Each test asserts the specific defect is fixed. Run them on top of the
unfixed code to see them fail — that is the verification that they are
testing real bugs, not aspirational properties.

Bugs covered:

- A. `cluster.set_tree_edit_threshold` is a module-level global. The
   `/api/clusters?mode=tree_edit&threshold=N` handler mutates it per-request,
   which races under concurrent FastAPI requests. Fix routes threshold
   through `list_clusters` directly.

- D. `repro.generate_repro` inlines DB-sourced `error_type` into pytest
   source. If `error_type` contains arbitrary code (a malformed importer or
   a malicious /v1/traces submission can set it), the generated test
   executes the injected code when run. Fix validates `error_type` is a
   plain dotted-identifier before inlining.

- R. `POST /v1/traces` reads the body with no size limit. A multi-GB body
   OOMs the dashboard process. Fix caps the body at
   `CLUSTERTRACE_OTLP_MAX_BYTES` (default 16 MiB) and returns 413.
"""
from __future__ import annotations

import threading
import time

import pytest

import clustertrace
from clustertrace import cluster, repro, storage

# --- Bug A: race on tree_edit threshold ------------------------------------


def _build_two_clusters_with_distance(n_traces_each: int = 4) -> None:
    """Seed the DB so the same trace set forms 1 cluster at threshold=10 and
    2 clusters at threshold=1. Used by the race-condition test below.
    """

    @clustertrace.trace
    def short_path() -> None:
        with clustertrace.span("a"):
            pass
        with clustertrace.span("b"):
            pass

    @clustertrace.trace
    def long_path() -> None:
        # 6 tokens further apart from short_path
        with clustertrace.span("a"):
            pass
        with clustertrace.span("b"):
            pass
        with clustertrace.span("c"):
            pass
        with clustertrace.span("d"):
            pass
        with clustertrace.span("e"):
            pass
        with clustertrace.span("f"):
            pass
        with clustertrace.span("g"):
            pass
        with clustertrace.span("h"):
            pass

    for _ in range(n_traces_each):
        short_path()
    for _ in range(n_traces_each):
        long_path()


def test_list_clusters_accepts_explicit_threshold_kwarg() -> None:
    """`list_clusters(mode='tree_edit', threshold=N)` must work without
    touching the module-level override. This is the affordance the dashboard
    needs to avoid the race entirely.
    """
    _build_two_clusters_with_distance()
    # No global override anywhere — pass threshold directly.
    one_cluster = cluster.list_clusters(mode="tree_edit", threshold=10)
    two_clusters = cluster.list_clusters(mode="tree_edit", threshold=1)
    assert len(one_cluster) == 1
    assert len(two_clusters) == 2
    # Crucially, the module-level override must still be None — we did not
    # touch it.
    assert cluster._tree_edit_threshold_override is None


def test_dashboard_threshold_param_is_concurrency_safe() -> None:
    """Two concurrent requests with different thresholds must each receive
    the result for their own threshold — not race on a shared global.
    """
    from fastapi.testclient import TestClient

    from clustertrace.dashboard.app import app

    _build_two_clusters_with_distance()
    client = TestClient(app)

    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def hit(threshold: int, key: str) -> None:
        barrier.wait()  # release both threads simultaneously
        r = client.get(
            f"/api/clusters?mode=tree_edit&threshold={threshold}&backfill=false"
        )
        results[key] = len(r.json()["clusters"])

    t1 = threading.Thread(target=hit, args=(10, "t10"))
    t2 = threading.Thread(target=hit, args=(1, "t1"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["t10"] == 1, f"threshold=10 should give 1 cluster, got {results['t10']}"
    assert results["t1"] == 2, f"threshold=1 should give 2 clusters, got {results['t1']}"
    # And the global must be back to None — the handler must not leak state.
    assert cluster._tree_edit_threshold_override is None


# --- Bug D: code injection via error_type ----------------------------------


def _seed_failing_trace(error_type_in_db: str) -> str:
    """Insert one finished failing trace with the given (raw) error_type and
    return its trace_id. Bypasses the public @trace decorator so we can
    inject a value that the API would not normally produce.
    """
    tid = "redteam-injection-trace"
    storage.insert_trace(tid, "victim", time.time())
    # One root span (so repro can find input_json) plus one child to give us
    # a real signature row.
    storage.insert_span(
        span_id=tid + ":root",
        trace_id=tid,
        parent_id=None,
        name="victim",
        kind="function",
        started_at=time.time(),
        input_data={"args": [1], "kwargs": {}},
        attrs={},
    )
    storage.finish_span(tid + ":root", time.time(), "error")
    storage.insert_span(
        span_id=tid + ":child",
        trace_id=tid,
        parent_id=tid + ":root",
        name="step",
        kind="function",
        started_at=time.time(),
        input_data=None,
        attrs={},
    )
    storage.finish_span(tid + ":child", time.time(), "error")
    storage.finish_trace(
        tid,
        time.time(),
        "error",
        error_type=error_type_in_db,
        error_message="boom",
    )
    cluster.compute_and_store_signature(tid)
    return tid


def test_repro_rejects_injection_in_error_type() -> None:
    """A DB-sourced error_type carrying arbitrary code must not be inlined
    as executable code in the generated pytest source.

    Two attack shapes are checked:
      (a) malformed identifier that, naively interpolated into
          `pytest.raises({err_name})`, executes inside the parens.
      (b) `\"\"\"` smuggled into a field that the old header concatenated into
          a docstring — would terminate the docstring early and let the
          rest of the payload become module-level code.
    """
    import ast
    import io
    import tokenize

    def _executable_code(src: str) -> str:
        """Return `src` with comments and string literals stripped — what
        actually runs when Python evaluates the file."""
        out_tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out_tokens.append(tok)
        return tokenize.untokenize(out_tokens)

    # (a) Identifier injection
    payload = "Exception); import os; os.system('echo PWNED'); raise ValueError(("
    tid = _seed_failing_trace(payload)
    src = repro.generate_repro(tid, "victim_module:victim_fn", mode="negative")
    # Source must compile.
    compile(src, "<generated>", "exec")
    # Source AST must contain `pytest.raises(Exception)` — not the payload.
    tree = ast.parse(src)
    raises_args = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "raises"
        ):
            raises_args.extend(node.args)
    assert any(isinstance(a, ast.Name) and a.id == "Exception" for a in raises_args), (
        "expected pytest.raises(Exception) — injected payload bled through"
    )
    # Strongest check: in the *executable* code (no comments, no strings),
    # the dangerous tokens must not appear.
    executable = _executable_code(src)
    assert "os.system" not in executable
    assert "import os" not in executable

    # (b) Docstring escape
    with storage.connect() as conn:
        conn.execute("DELETE FROM traces WHERE id = ?", (tid,))
        conn.execute("DELETE FROM spans WHERE trace_id = ?", (tid,))
        conn.commit()
    breaker = '"""\nimport os\nos.system("touch /tmp/PWNED")\n"""'
    tid2 = _seed_failing_trace(breaker)
    src2 = repro.generate_repro(tid2, "m:f", mode="negative")
    compile(src2, "<generated>", "exec")
    executable2 = _executable_code(src2)
    assert "os.system" not in executable2, (
        f"docstring-escape payload reached executable code:\n{executable2}"
    )
    assert "import os" not in executable2


def test_repro_accepts_plain_identifier_error_type() -> None:
    """Real-world error type names (e.g. 'ValueError', 'requests.HTTPError')
    must still flow through unchanged."""
    for good in ("ValueError", "MyPkg.Custom"):
        tid = _seed_failing_trace(good)
        # Re-insert with a fresh id per case
        src = repro.generate_repro(tid, "m:f", mode="negative")
        assert f"pytest.raises({good})" in src
        compile(src, "<generated>", "exec")
        # Clean up for next loop iteration
        with storage.connect() as conn:
            conn.execute("DELETE FROM traces WHERE id = ?", (tid,))
            conn.execute("DELETE FROM spans WHERE trace_id = ?", (tid,))
            conn.commit()


# --- Bug R: OTLP /v1/traces unbounded body --------------------------------


def test_otlp_endpoint_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized OTLP/JSON body must be rejected with 413 before any
    parsing — not silently allocated into memory.
    """
    monkeypatch.setenv("CLUSTERTRACE_OTLP_MAX_BYTES", "1024")
    from fastapi.testclient import TestClient

    from clustertrace.dashboard.app import app

    client = TestClient(app)
    # 4 KiB body, well above the 1 KiB cap. Use Content-Length so the cap
    # can be enforced without buffering.
    big = b'{"resourceSpans":[' + (b'{"a":"' + b'x' * 200 + b'"},') * 20 + b']}'
    assert len(big) > 1024
    r = client.post(
        "/v1/traces",
        content=big,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text[:200]}"


def test_otlp_endpoint_accepts_normal_body() -> None:
    """A small valid OTLP/JSON body must still be accepted (regression
    guard so the size cap doesn't break the happy path)."""
    from fastapi.testclient import TestClient

    from clustertrace.dashboard.app import app

    client = TestClient(app)
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "test.span",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000123000000",
                                "kind": 1,
                                "status": {"code": 1},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    r = client.post("/v1/traces", json=payload)
    assert r.status_code == 200, r.text
