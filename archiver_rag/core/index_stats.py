"""Read-only comparison of the ChromaDB index against the notes on disk.

`archiver-rag health` used to report `collection.count()` and nothing else, which
cannot distinguish a healthy index from one missing six notes — the exact failure the
watcher's atomic-save bug produced (three notes on disk, absent from the index, found
only by hand). This module answers the question that count was standing in for: does
the index still describe the vault?

Nothing here writes. The two drift categories map onto existing repair commands:
`missing_from_index` → `archiver-rag sync`, `orphaned_in_index` → `archiver-rag prune`.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from archiver_rag.utils import is_indexable_note

# Drift lists are for a human reading a terminal, not for reconciliation — the counts
# alongside them are always exact.
MAX_LISTED = 25


def _indexed_sources() -> tuple[dict[str, float], int]:
    """Map of vault-relative source → indexed mtime, plus the raw chunk count.

    Dedupes by `meta["source"]` the same way prune_orphans and sync_vault do: one note
    contributes many chunks, all carrying identical metadata.
    """
    from archiver_rag.core.db import collection

    chunks = collection.count()
    result = collection.get(include=["metadatas"])
    indexed: dict[str, float] = {}
    for meta in result.get("metadatas") or []:
        source = meta.get("source")
        if not source or source in indexed:
            continue
        mtime = meta.get("mtime")
        indexed[source] = float(mtime) if mtime is not None else 0.0
    return indexed, chunks


def index_stats(vault: Path) -> dict:
    """Index-vs-disk drift report. Never raises — Chroma failures land in `error`.

    `collection` is a lazy proxy whose *first* attribute access opens the database, so
    an unconfigured or corrupt install raises here and nowhere earlier. health must
    still be able to report on the vault side in that case, so the error is returned
    rather than propagated.
    """
    on_disk: dict[str, float] = {}
    for f in vault.rglob("*.md"):
        if not is_indexable_note(f):
            continue
        try:
            on_disk[str(f.relative_to(vault))] = float(os.path.getmtime(f))
        except OSError:
            continue

    stats: dict = {
        "chunks": 0,
        "indexed_notes": 0,
        "notes_on_disk": len(on_disk),
        "missing_from_index": [],
        "orphaned_in_index": [],
        "stale": [],
        "counts": {"missing_from_index": 0, "orphaned_in_index": 0, "stale": 0},
        "newest_mtime": None,
        "error": None,
    }

    try:
        indexed, chunks = _indexed_sources()
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        return stats

    missing = sorted(set(on_disk) - set(indexed))
    orphaned = sorted(set(indexed) - set(on_disk))
    # int() on the disk mtime: ingest stores int(os.path.getmtime), so comparing against
    # the raw float would flag every note with a sub-second remainder as stale forever.
    stale = sorted(
        src
        for src, disk_mtime in on_disk.items()
        if src in indexed and int(disk_mtime) > indexed[src]
    )

    newest = max(indexed.values(), default=0.0)

    stats.update(
        {
            "chunks": chunks,
            "indexed_notes": len(indexed),
            "missing_from_index": missing[:MAX_LISTED],
            "orphaned_in_index": orphaned[:MAX_LISTED],
            "stale": stale[:MAX_LISTED],
            "counts": {
                "missing_from_index": len(missing),
                "orphaned_in_index": len(orphaned),
                "stale": len(stale),
            },
            "newest_mtime": (
                datetime.fromtimestamp(newest).isoformat(timespec="seconds")
                if newest
                else None
            ),
        }
    )
    return stats


def in_sync(stats: dict) -> bool:
    """True when the index is a faithful picture of the vault."""
    counts = stats.get("counts") or {}
    return not stats.get("error") and not any(counts.values())
