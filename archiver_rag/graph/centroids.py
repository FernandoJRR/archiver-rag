"""Declared folder centroids — embeddings of _folder.md description_terms (§2.3).

The cache is keyed by a **fingerprint of the description text**, not by mtime and not by
a dirty flag. That is what makes "the embedding follows _folder.md" structural rather
than procedural: edit the sidecar from Obsidian, from vim, or with a script while the
service is stopped, and the terms change, so the fingerprint changes, so the next lookup
embeds afresh. A missed watcher event costs a few milliseconds, never a wrong placement.
The watcher hook exists for warm-up and for the log line, not to keep this honest.

vault: Path is always explicit and get_vault_path() is never bound at module level, so
this module stays out of conftest._MODULES_WITH_VAULT.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from archiver_rag.vault.folder_notes import (
    FolderNote,
    described_folders,
    read_folder_note,
)


def _cache_path() -> Path:
    """Single indirection so tests redirect the cache away from the real install."""
    from archiver_rag import paths

    return paths.centroids_path()


def description_text(note: FolderNote) -> str:
    """The text that actually gets embedded.

    description_terms only. `distinctive` is deliberately excluded: it is highest-raw-IDF
    and explicitly not MMR-diversified, so it carries noise — decision/ currently lists
    `rbac` as distinctive, and embedding that would pull RBAC notes into decision/, i.e.
    feed the very gravity well this replaces. The human-readable prose line is skipped
    too; its Spanish scaffolding adds no signal and mismatches the English terms.
    """
    return ", ".join(note.description_terms)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _unit(vec) -> np.ndarray:
    """Normalize to unit length. embed() returns unnormalized vectors."""
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr if norm == 0.0 else arr / norm


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Dot product of two vectors that _unit has already normalized."""
    return float(np.dot(a, b))


def weighted_cosine(channels: list[tuple[float, np.ndarray | None, np.ndarray | None]]) -> float:
    """Weighted sum of cosine similarities across N channels.

    A channel missing either vector is dropped and the remaining weights are
    renormalized to sum to 1 — generalizes suggest_folder's original "no body ->
    identity-only, effective weight 1.0" fallback to any number of channels.
    Shared by graph/placement.py::suggest_folder (note identity+content vs. a single
    folder centroid) and graph/inbox.py's note-vs-note similarity — same formula,
    different vectors passed in, so the weighting logic lives in exactly one place.
    """
    valid = [(w, a, b) for w, a, b in channels if a is not None and b is not None]
    if not valid:
        return 0.0
    total_w = sum(w for w, _, _ in valid)
    return sum((w / total_w) * cosine(a, b) for w, a, b in valid)


# ──────────────────────────────────────────────────────────────────────────────
# Cache I/O — every path fails soft. A cache is an optimization, never a source
# of truth, so an unreadable or unwritable one must only cost re-embedding.
# ──────────────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def _entry_vec(entry, expected_fp: str) -> np.ndarray | None:
    """Reusable cached vector, or None if the entry is stale or malformed."""
    if not isinstance(entry, dict) or entry.get("fp") != expected_fp:
        return None
    vec = entry.get("vec")
    if not isinstance(vec, list) or not vec:
        return None
    return _unit(vec)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def cached_centroids() -> dict[str, str]:
    """Cached folder → fingerprint, straight off disk. Never embeds, never writes.

    folder_centroids() re-embeds every stale or missing folder as a side effect of being
    called, which is right for placement and wrong for a status readout — `status` must
    stay cheap and must not mutate the cache just by being run. This reports what is
    cached right now, nothing more.
    """
    cache = _load_cache()
    return {
        k: v.get("fp", "")
        for k, v in cache.items()
        if isinstance(v, dict)
    }


def folder_centroids(vault: Path) -> dict[str, np.ndarray]:
    """{vault-relative folder: unit centroid} for every folder with a description.

    Folders with no sidecar, or a sidecar with empty description_terms, are absent —
    §3.5's lazy policy: no description, no participation in placement. Keys are full
    vault-relative paths so subfolders are independent candidates (§3 "Nivel 2").
    """
    wanted: dict[str, str] = {}
    for rel_folder, note in described_folders(vault).items():
        text = description_text(note)
        if text:
            wanted[rel_folder] = text

    cache = _load_cache()
    centroids: dict[str, np.ndarray] = {}
    missing: list[str] = []

    for rel_folder, text in wanted.items():
        cached = _entry_vec(cache.get(rel_folder), fingerprint(text))
        if cached is None:
            missing.append(rel_folder)
        else:
            centroids[rel_folder] = cached

    if missing:
        from archiver_rag.core.embedder import embed

        # One batched call — a cold cache costs a single model round trip, not N.
        vecs = embed([wanted[f] for f in missing])
        for rel_folder, vec in zip(missing, vecs):
            centroids[rel_folder] = _unit(vec)
            cache[rel_folder] = {
                "fp": fingerprint(wanted[rel_folder]),
                "vec": list(vec),
            }

    stale = [k for k in cache if k not in wanted]
    for k in stale:
        del cache[k]

    if missing or stale:
        _save_cache(cache)

    return centroids


def refresh_centroid(vault: Path, rel_folder: str) -> bool:
    """Recompute one folder's centroid. True if the stored vector actually changed.

    Called by the watcher when _folder.md is written. Returning False for an unchanged
    description keeps the log quiet when a save did not touch the terms.
    """
    cache = _load_cache()
    entry = cache.get(rel_folder)
    old_fp = entry.get("fp") if isinstance(entry, dict) else None

    note = read_folder_note(vault, rel_folder)
    text = description_text(note) if note is not None else ""

    if not text:
        # Sidecar gone, unparseable, or emptied — the folder stops participating.
        if rel_folder in cache:
            del cache[rel_folder]
            _save_cache(cache)
            return True
        return False

    fp = fingerprint(text)
    if fp == old_fp:
        return False

    from archiver_rag.core.embedder import embed

    cache[rel_folder] = {"fp": fp, "vec": list(embed([text])[0])}
    _save_cache(cache)
    return True


def drop_centroid(rel_folder: str) -> bool:
    """Forget one folder's centroid. True if there was something to forget."""
    cache = _load_cache()
    if rel_folder not in cache:
        return False
    del cache[rel_folder]
    _save_cache(cache)
    return True
