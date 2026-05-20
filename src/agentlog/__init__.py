"""agentlog — zero-config local-first instrumentation for LLM agents."""

from agentlog.trace import span, tool_call, trace
from agentlog.storage import get_db_path

__all__ = ["trace", "span", "tool_call", "wrap_anthropic", "get_db_path"]
__version__ = "0.1.0"


def wrap_anthropic(client):
    """Wrap an Anthropic client so that messages.create calls are logged.

    Explicit wrap — does not monkey-patch globally. Pass the wrapped client
    where you would have passed the original.
    """
    from agentlog.anthropic import wrap_anthropic as _wrap
    return _wrap(client)
