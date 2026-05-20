"""agentlog — zero-config local-first instrumentation for LLM agents."""

from agentlog.storage import get_db_path
from agentlog.trace import metric, span, tag, tool_call, trace

__all__ = [
    "trace",
    "span",
    "tool_call",
    "tag",
    "metric",
    "wrap_anthropic",
    "wrap_openai",
    "get_db_path",
]
__version__ = "0.3.0"


def wrap_anthropic(client):
    """Wrap an Anthropic client so that messages.create calls are logged.

    Explicit wrap — does not monkey-patch globally. Pass the wrapped client
    where you would have passed the original.
    """
    from agentlog.anthropic import wrap_anthropic as _wrap
    return _wrap(client)


def wrap_openai(client):
    """Wrap an OpenAI client so that chat.completions.create calls are logged.

    Explicit wrap — does not monkey-patch globally.
    """
    from agentlog.openai import wrap_openai as _wrap
    return _wrap(client)
