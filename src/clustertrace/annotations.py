"""Cluster annotations — mark a cluster as expected-failure / wontfix / etc.

The dashboard's "failure summary" excludes clusters annotated as
`expected-failure` so a known-broken cluster stops dominating the headline.
Annotations are advisory — they never trigger deletion or mute data.
"""
from __future__ import annotations

from typing import Any

from clustertrace import storage

ANNOTATION_STATUSES = frozenset({
    "expected-failure",
    "wontfix",
    "priority",
    "acceptable",
})
NOTE_MAX_LEN = 4096


def annotate_cluster(
    sig_hash: str,
    *,
    status: str | None = None,
    note: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Attach (or update) metadata on a cluster identified by `sig_hash`.

    Pass any combination of:
      - `status`: one of `expected-failure`, `wontfix`, `priority`, `acceptable`.
        Pass `None` to leave the existing status alone; use `clear_annotation`
        to remove an annotation entirely.
      - `note`: free-form text up to 4096 chars. Pass empty string to clear it.
      - `tag`: appended to the cluster's tag list (idempotent).

    Returns the updated annotation row.
    """
    if status is None and note is None and tag is None:
        raise ValueError(
            "annotate_cluster needs at least one of status, note, or tag set"
        )
    return storage.upsert_cluster_annotation(
        sig_hash, status=status, note=note, tag=tag
    )


def clear_annotation(sig_hash: str) -> bool:
    """Remove the annotation entirely. Returns True iff something was removed."""
    return storage.clear_cluster_annotation(sig_hash)


def get_annotation(sig_hash: str) -> dict[str, Any] | None:
    return storage.get_cluster_annotation(sig_hash)


def all_annotations() -> list[dict[str, Any]]:
    return storage.list_cluster_annotations()


def is_expected_failure(sig_hash: str) -> bool:
    ann = get_annotation(sig_hash)
    return bool(ann and ann.get("status") == "expected-failure")


def expected_failure_sig_hashes() -> set[str]:
    """Bulk lookup used by the dashboard's failure-summary filter."""
    return {
        a["sig_hash"]
        for a in all_annotations()
        if a.get("status") == "expected-failure"
    }
