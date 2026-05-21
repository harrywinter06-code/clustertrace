"""clustertrace — zero-config local-first instrumentation for LLM agents."""
import atexit as _atexit

from clustertrace.storage import get_db_path
from clustertrace.trace import metric, span, tag, tool_call, trace

__all__ = [
    "trace",
    "span",
    "tool_call",
    "tag",
    "metric",
    "flush",
    "wrap_anthropic",
    "wrap_openai",
    "get_db_path",
]
__version__ = "0.6.0"


def flush() -> int:
    """Best-effort cleanup of orphan traces before process exit.

    Synchronous, idempotent. Returns the number of traces finalized.
    """
    from clustertrace.maintenance import flush as _flush
    return _flush()


# Register a shutdown hook so a crashed/killed process at least leaves the DB
# in a coherent state next time the dashboard reads it.
def _shutdown_cleanup() -> None:
    try:
        flush()
    except Exception:
        pass


_atexit.register(_shutdown_cleanup)


def wrap_anthropic(client):
    """Wrap an Anthropic client so that messages.create calls are logged.

    Explicit wrap — does not monkey-patch globally. Pass the wrapped client
    where you would have passed the original.
    """
    from clustertrace.anthropic import wrap_anthropic as _wrap
    return _wrap(client)


def wrap_openai(client):
    """Wrap an OpenAI client so that chat.completions.create calls are logged.

    Explicit wrap — does not monkey-patch globally.
    """
    from clustertrace.openai import wrap_openai as _wrap
    return _wrap(client)
