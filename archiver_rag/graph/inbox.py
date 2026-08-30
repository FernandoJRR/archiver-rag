"""Gate 2 — inbox clustering by embedding similarity (never wikilink-graph topology).

Notes with no semantic-placement match and no `type:` fallback either
(`suggest_folder`'s `reason == "none"`) land in `inbox/` (see
`watcher.py::VaultHandler._maybe_cluster`). This module groups the notes sitting there
by cosine similarity over their identity+content embeddings and spins a group out into
a brand-new real folder once it reaches `inbox_min_cluster_size`.

Deliberately NOT wikilink-graph clustering. The 2026-08-20 folder-collapse incident was
label propagation over a dense `auto_link` graph collapsing into giant communities —
that pathology reproduces at inbox scale with any graph-topology approach, even
post-desaturation, because it isn't about density, it's about the substrate: a
wikilink graph doesn't discriminate meaning, only connectivity. Embeddings don't share
that failure mode.

Greedy cosine-threshold connected components, not k-means/HDBSCAN: the inbox holds a
handful of notes at a time in a personal vault this size, so a pre-chosen k (k-means)
or density assumptions under ~20 points (HDBSCAN) are a worse fit than a threshold
check that needs no new dependency and is trivial to explain and test. Known tradeoff,
accepted deliberately: this is single-link clustering, which can chain (A-B similar,
B-C similar, A-C not, all three still grouped). Flagged for re-measurement once
`auto_inbox` sees live use, same treatment every other new constant in this codebase
gets (`link_margin`, `folder_vacancy_grace_periods`, ...).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from archiver_rag.utils import extract_frontmatter, is_indexable_note
from archiver_rag.graph import centroids as _centroids_mod
from archiver_rag.graph.placement import note_identity_text, note_content_text


def _note_vectors(
    vault: Path, paths: list[Path]
) -> dict[Path, tuple[np.ndarray | None, np.ndarray | None]]:
    """Batch-embed identity+content text for every note in `paths`.

    One embed() call for every non-empty text across the whole batch, mirroring
    suggest_folder's own batching. A note with unreadable/empty identity text (should
    not happen for a real indexed note, but embed() cannot be called on "") gets
    identity_vec=None — weighted_cosine then drops it from any comparison, so such a
    note simply never joins a group rather than raising.
    """
    identity_texts = [note_identity_text(p) for p in paths]
    content_texts = [note_content_text(p) for p in paths]
    all_texts = identity_texts + content_texts

    non_empty_idx = [i for i, t in enumerate(all_texts) if t]
    vec_by_idx: dict[int, np.ndarray] = {}
    if non_empty_idx:
        from archiver_rag.core.embedder import embed

        vecs = embed([all_texts[i] for i in non_empty_idx])
        vec_by_idx = {i: _centroids_mod._unit(v) for i, v in zip(non_empty_idx, vecs)}

    n = len(paths)
    return {p: (vec_by_idx.get(i), vec_by_idx.get(n + i)) for i, p in enumerate(paths)}


def _pairwise_similarity(
    vectors: dict[Path, tuple[np.ndarray | None, np.ndarray | None]],
    w_identity: float,
    w_content: float,
) -> dict[tuple[Path, Path], float]:
    """sim(a, b) via the same weighted_cosine primitive suggest_folder uses — just
    called with two notes' vectors instead of a note's vectors against a folder
    centroid."""
    paths = list(vectors)
    sims: dict[tuple[Path, Path], float] = {}
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            a, b = paths[i], paths[j]
            id_a, content_a = vectors[a]
            id_b, content_b = vectors[b]
            sims[(a, b)] = _centroids_mod.weighted_cosine(
                [(w_identity, id_a, id_b), (w_content, content_a, content_b)]
            )
    return sims


def group_inbox_notes(
    vault: Path,
    paths: list[Path],
    *,
    threshold: float,
    w_identity: float = 0.6,
    w_content: float = 0.4,
) -> list[list[Path]]:
    """Connected components over pairs whose weighted cosine similarity clears threshold.

    Single-link clustering: simple, deterministic, explainable at inbox scale, no new
    dependency. Returns every component including singletons — filtering by
    `inbox_min_cluster_size` is the caller's job, so this stays a pure,
    independently-testable grouping step.
    """
    if not paths:
        return []

    vectors = _note_vectors(vault, paths)
    sims = _pairwise_similarity(vectors, w_identity, w_content)

    parent = {p: p for p in paths}

    def find(p: Path) -> Path:
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    def union(a: Path, b: Path) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (a, b), sim in sims.items():
        if sim >= threshold:
            union(a, b)

    groups: dict[Path, list[Path]] = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)
    return list(groups.values())


def name_cluster(
    paths: list[Path], *, max_terms: int = 6, mmr_lambda: float = 0.5
) -> tuple[list[str], list[str]]:
    """(description_terms, distinctive) for a cluster's notes, computed in-memory
    before any file exists in a real folder.

    Reuses graph.terms._terms_by_tags_corpus (tag-frequency scoring) directly — inbox
    clusters are always small (>= inbox_min_cluster_size), the same "small folder"
    strategy log_note already uses at n=1. Deliberately does not attempt c-TF-IDF here:
    that needs describable_folders()'s on-disk corpus, which doesn't exist yet for a
    cluster that hasn't been moved anywhere. This function only picks a slug for the
    destination path — maybe_spin_out_clusters re-derives the folder's real
    description afterward via extract_terms, once the notes are physically there.
    """
    from archiver_rag.graph.terms import _note_tags, _terms_by_tags_corpus

    tags: list[str] = []
    for p in paths:
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        fm, _body = extract_frontmatter(raw)
        tags.extend(_note_tags(fm))

    return _terms_by_tags_corpus(tags, max_terms, mmr_lambda)


def _resolve_cluster_folder(vault: Path, slug: str) -> str:
    """Collision-safe destination folder name, mirroring vault/notes.py::
    _resolve_filepath's counter pattern."""
    slug = slug or "inbox-cluster"
    candidate = slug
    counter = 1
    while (vault / candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1
    return candidate


def maybe_spin_out_clusters(
    vault: Path,
    *,
    min_cluster_size: int = 3,
    threshold: float = 0.5,
    w_identity: float = 0.6,
    w_content: float = 0.4,
    max_terms: int = 6,
    mmr_lambda: float = 0.5,
) -> list[dict]:
    """Check inbox/ for a cluster ready to become a real folder, and spin it out.

    Event-driven, not periodic: called by watcher.py::_maybe_cluster right after a
    note lands in inbox/. No timer/thread exists anywhere in this codebase by
    deliberate design — this follows the same convention as Gate 1's empty_sweeps
    (piggyback on an existing structural-change trigger rather than a clock).

    Returns [{"folder": rel_folder, "notes": [rel_paths]}, ...] for every newly
    created folder. `notes` lists each note's PRE-move, inbox-relative path (e.g.
    "inbox/lonely.md"), not its new destination — the caller only knows a note by
    where it just placed it into inbox/, so that's what it needs to check "was the
    note that triggered this event itself promoted out of inbox in the same pass."
    """
    inbox_dir = vault / "inbox"
    if not inbox_dir.is_dir():
        return []

    paths = sorted(
        f for f in inbox_dir.iterdir() if f.is_file() and is_indexable_note(f)
    )
    if len(paths) < min_cluster_size:
        return []

    groups = group_inbox_notes(
        vault, paths, threshold=threshold, w_identity=w_identity, w_content=w_content
    )
    qualifying = [g for g in groups if len(g) >= min_cluster_size]
    if not qualifying:
        return []

    from archiver_rag.graph.terms import extract_terms
    from archiver_rag.vault.folder_notes import apply_extracted_terms
    from archiver_rag.vault.notes import _slugify
    from archiver_rag.vault.reorganize import move_notes

    spun_out: list[dict] = []
    for group in qualifying:
        desc_terms, _dist = name_cluster(group, max_terms=max_terms, mmr_lambda=mmr_lambda)
        slug = _slugify(desc_terms[0]) if desc_terms else ""
        rel_folder = _resolve_cluster_folder(vault, slug or "inbox-cluster")

        pre_move_rel_paths = [str(p.relative_to(vault)) for p in group]
        moves = [
            {"source": rel, "destination": f"{rel_folder}/{p.name}"}
            for rel, p in zip(pre_move_rel_paths, group)
        ]
        result = move_notes(moves)
        if not result.get("moved"):
            continue

        # Gate 1 birth idiom, verbatim (see watcher.py::_maybe_cluster and
        # vault/notes.py::log_note): the folder is now real on disk, so extract_terms
        # reads it directly rather than relying on name_cluster's in-memory estimate.
        desc, dist = extract_terms(vault, rel_folder, max_terms=max_terms, mmr_lambda=mmr_lambda)
        apply_extracted_terms(vault, rel_folder, desc, dist, max_terms=max_terms)

        spun_out.append({"folder": rel_folder, "notes": pre_move_rel_paths})

    return spun_out
