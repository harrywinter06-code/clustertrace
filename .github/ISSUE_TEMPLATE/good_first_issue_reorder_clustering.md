---
name: 'Wanted: tree-edit-distance clustering mode'
about: Help wanted — better clustering for traces that differ by reorderings
title: 'Add mode=''tree_edit'' to signature_for_spans (reorder + insertion tolerance)'
labels: enhancement, help wanted, algorithm
assignees: ''
---

## What

Today agentlog has two clustering modes:
- `mode='ordered'` — exact-string equality on RLE-collapsed sequence
- `mode='set'` — sorted unique pairs (collapses all reorderings)

Both are extremes. We want a middle ground: a signature mode that tolerates small reorderings and insertions (e.g. one extra retry, two tool calls swapped) but still distinguishes structurally different paths.

## Why

Real agents are noisy. With `mode='ordered'` you get 100+ clusters from 1000 traces (one extra retry creates a new cluster). With `mode='set'` you collapse everything that has the same step inventory regardless of order — too coarse.

Tree-edit-distance or Levenshtein on the step sequence would give a tunable threshold.

## Design sketch

```python
def signature_for_spans(spans, mode='ordered', edit_distance_threshold=None):
    if mode == 'tree_edit':
        # Compute a normalized step sequence
        # Bucket traces whose pairwise edit distance < threshold
        ...
```

Storage implication: the canonical signature for a cluster becomes the "representative" sequence; non-canonical traces store a reference. This needs a `signature_canonical_id` column on traces (v4 migration).

## Pointers

- [`src/agentlog/cluster.py`](../../src/agentlog/cluster.py).
- [Wagner-Fischer algorithm](https://en.wikipedia.org/wiki/Wagner%E2%80%93Fischer_algorithm) is the obvious starting point.
- [Zhang-Shasha](https://epubs.siam.org/doi/10.1137/0218082) for the tree-edit-distance variant if you want hierarchical structure too.
- This is real algorithmic work; not a 4-hour PR.

Estimated effort: a weekend of focused work + a benchmark showing it's better than `mode='set'` on real data.
