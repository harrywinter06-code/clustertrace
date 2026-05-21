"""LlamaIndex → agentlog via OpenTelemetry.

LlamaIndex's instrumentation module emits OTel spans for queries, retrievers,
and LLM calls. The agentlog SpanExporter ingests them so the same clusters /
cost / search views work for LlamaIndex apps.

Setup:
    pip install agentlog llama-index llama-index-llms-anthropic \
        opentelemetry-sdk

Run:
    ANTHROPIC_API_KEY=sk-ant-... python examples/llamaindex_example.py
    agentlog dashboard
"""
from __future__ import annotations

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentlog.otel import AgentlogSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(AgentlogSpanExporter()))
otel_trace.set_tracer_provider(provider)


def main() -> None:
    # LlamaIndex's instrumentation hooks for OTel
    from llama_index.core import Document, VectorStoreIndex
    from llama_index.core.instrumentation import get_dispatcher
    from llama_index.llms.anthropic import Anthropic as LIAnthropic

    # Route LlamaIndex's instrumentation events into OTel spans.
    # (See llama-index docs for the exact span-event bridge for your version.)
    get_dispatcher()  # event-handler wiring goes here; depends on your llama-index version

    docs = [
        Document(text="Postgres uses MVCC for concurrent reads/writes."),
        Document(text="Redis is single-threaded but extremely fast for in-memory ops."),
        Document(text="Kubernetes uses etcd as a distributed key-value store."),
    ]
    index = VectorStoreIndex.from_documents(docs)
    llm = LIAnthropic(model="claude-haiku-4-5-20251001")
    query_engine = index.as_query_engine(llm=llm)

    for q in [
        "How does Postgres handle concurrent writes?",
        "What's special about Redis storage?",
        "How does Kubernetes store cluster state?",
    ]:
        resp = query_engine.query(q)
        print(f"Q: {q}\nA: {str(resp)[:120]}…\n")

    provider.shutdown()


if __name__ == "__main__":
    main()
