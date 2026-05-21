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
    "annotate_cluster",
    "clear_cluster_annotation",
    "get_cluster_annotation",
]
__version__ = "0.9.0"


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


def annotate_cluster(
    sig_hash: str,
    *,
    status: str | None = None,
    note: str | None = None,
    tag: str | None = None,
):
    """Mark a cluster with metadata that survives across runs.

    See `clustertrace.annotations.annotate_cluster` for the full contract.
    """
    from clustertrace.annotations import annotate_cluster as _annotate
    return _annotate(sig_hash, status=status, note=note, tag=tag)


def clear_cluster_annotation(sig_hash: str) -> bool:
    """Remove a cluster annotation entirely."""
    from clustertrace.annotations import clear_annotation
    return clear_annotation(sig_hash)


def get_cluster_annotation(sig_hash: str):
    """Read the current annotation for a cluster (or None)."""
    from clustertrace.annotations import get_annotation
    return get_annotation(sig_hash)
