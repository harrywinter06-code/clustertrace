"""Overhead-per-trace benchmark — concrete numbers for the README.

Compares uninstrumented function calls against @clustertrace.trace across a few
workload shapes, then prints a Markdown table you can paste into a comparison
post or PR.

Run:
    python examples/benchmark.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time

os.environ["CLUSTERTRACE_DB"] = tempfile.mktemp(suffix="-bench.db")

import clustertrace  # noqa: E402
from clustertrace import maintenance, storage  # noqa: E402


def _fn(x):  # baseline, no decoration
    return x * 2


@clustertrace.trace
def _traced(x):
    return x * 2


@clustertrace.trace
def _nested(x):
    with clustertrace.span("inner"):
        clustertrace.tool_call("lookup", args={"x": x}, result={"y": x * 2})
    return x * 2


async def _async_fn(x):
    await asyncio.sleep(0)
    return x * 2


@clustertrace.trace
async def _async_traced(x):
    await asyncio.sleep(0)
    return x * 2


def _bench(label: str, fn, n: int = 2000) -> tuple[float, float]:
    """Returns (mean_us, p95_us)."""
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn(1)
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    return statistics.mean(samples), samples[int(n * 0.95)]


async def _bench_async(label: str, fn, n: int = 2000) -> tuple[float, float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await fn(1)
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    return statistics.mean(samples), samples[int(n * 0.95)]


def main() -> None:
    storage.reset_initialized_cache()

    print(
        "Warming up (500 traced calls, ~30s on Linux/macOS, "
        "~2min on Windows NTFS — SQLite per-call write dominates)…",
        flush=True,
    )
    for _ in range(500):
        _traced(1)
        _nested(1)

    rows = []
    print("  running: baseline sync fn (no decorator)", flush=True)
    rows.append(("baseline sync fn (no decorator)", *_bench("baseline", _fn)))
    print("  running: @clustertrace.trace sync", flush=True)
    rows.append(("@clustertrace.trace sync", *_bench("traced", _traced)))
    print("  running: @clustertrace.trace + span + tool_call", flush=True)
    rows.append(("@clustertrace.trace + span + tool_call", *_bench("nested", _nested)))
    print("  running: baseline async fn", flush=True)
    rows.append((
        "baseline async fn",
        *(asyncio.run(_bench_async("async-baseline", _async_fn))),
    ))
    print("  running: @clustertrace.trace async", flush=True)
    rows.append((
        "@clustertrace.trace async",
        *(asyncio.run(_bench_async("async-traced", _async_traced))),
    ))

    # 200-concurrent-async-traces throughput
    async def _burst():
        await asyncio.gather(*[_async_traced(i) for i in range(200)])
    t0 = time.perf_counter()
    asyncio.run(_burst())
    burst = time.perf_counter() - t0

    with storage.connect() as c:
        total = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        total_spans = c.execute("SELECT COUNT(*) FROM spans").fetchone()[0]

    print()
    print("| workload | mean | p95 |")
    print("|---|---:|---:|")
    for label, mean, p95 in rows:
        print(f"| {label} | {mean:.1f} µs | {p95:.1f} µs |")
    print()
    print(f"200 concurrent async traces (with pooling): **{burst*1000:.0f} ms** "
          f"({burst*1000/200:.1f} ms/trace amortized)")
    print(f"DB at end: {total} traces, {total_spans} spans.")

    # Cleanup
    maintenance.flush()


if __name__ == "__main__":
    main()
