"""Overhead-per-trace benchmark — concrete numbers for the README.

Compares uninstrumented function calls against @agentlog.trace across a few
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

os.environ["AGENTLOG_DB"] = tempfile.mktemp(suffix="-bench.db")

import agentlog  # noqa: E402
from agentlog import maintenance, storage  # noqa: E402


def _fn(x):  # baseline, no decoration
    return x * 2


@agentlog.trace
def _traced(x):
    return x * 2


@agentlog.trace
def _nested(x):
    with agentlog.span("inner"):
        agentlog.tool_call("lookup", args={"x": x}, result={"y": x * 2})
    return x * 2


async def _async_fn(x):
    await asyncio.sleep(0)
    return x * 2


@agentlog.trace
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

    print("Warming up…")
    for _ in range(500):
        _traced(1)
        _nested(1)

    rows = []
    rows.append(("baseline sync fn (no decorator)", *_bench("baseline", _fn)))
    rows.append(("@agentlog.trace sync", *_bench("traced", _traced)))
    rows.append(("@agentlog.trace + span + tool_call", *_bench("nested", _nested)))
    rows.append((
        "baseline async fn",
        *(asyncio.run(_bench_async("async-baseline", _async_fn))),
    ))
    rows.append((
        "@agentlog.trace async",
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
