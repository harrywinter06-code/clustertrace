"""LangChain → clustertrace in 5 lines via OpenTelemetry.

LangChain emits OpenTelemetry spans for chains, agents, and LLM calls. The
clustertrace SpanExporter ingests them into the same SQLite store as your
@clustertrace.trace functions, so the clusters page, cost view, and search
work across both native and LangChain-instrumented code.

Setup:
    pip install clustertrace langchain langchain-anthropic langchain-community \
        opentelemetry-sdk opentelemetry-instrumentation-langchain

Run:
    ANTHROPIC_API_KEY=sk-ant-... python examples/langchain_example.py
    clustertrace dashboard
"""
from __future__ import annotations

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from clustertrace.otel import ClustertraceSpanExporter

# The five lines that route everything LangChain emits into clustertrace.
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ClustertraceSpanExporter()))
otel_trace.set_tracer_provider(provider)

# Optional: turn on the OTel auto-instrumentor for LangChain so chain steps,
# tool calls, and LLM calls all become spans.
try:
    from opentelemetry.instrumentation.langchain import LangChainInstrumentor

    LangChainInstrumentor().instrument()
except ImportError:  # pragma: no cover
    print("note: install opentelemetry-instrumentation-langchain for auto-instrumentation")


# -- Your normal LangChain code from here on. Unchanged. --
def main() -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.prompts import ChatPromptTemplate

    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", max_tokens=200)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a concise research assistant."),
        ("human", "{question}"),
    ])
    chain = prompt | llm

    questions = [
        "What does sparse attention do to hallucination rates?",
        "When does best-of-N inference beat bigger models?",
        "What's the failure mode of long-horizon RLHF agents?",
    ]
    for q in questions:
        r = chain.invoke({"question": q})
        print(f"Q: {q}\nA: {r.content[:120]}…\n")

    # Make sure the BatchSpanProcessor flushes before the process exits.
    provider.shutdown()


if __name__ == "__main__":
    main()
