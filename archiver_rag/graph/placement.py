"""Semantic folder placement for a note — Stage B of spec §3.

replace the wikilink-neighbour vote (`cluster_note`) with cosine similarity against
declared folder descriptions (`folder_centroids`). This signal has no rich-get-richer
term: a folder does not become more similar to a note just by holding more notes.

vault: Path is always explicit, get_vault_path() never bound at module level.
"""

from __future__ import annotations

import re
from pathlib import Path

from archiver_rag.utils import extract_frontmatter, strip_related_section
from archiver_rag.graph import centroids as _centroids_mod


# ──────────────────────────────────────────────────────────────────────────────
# Note text to embed — two separate channels (Fase B, spec fortalecer-dominios)
# ──────────────────────────────────────────────────────────────────────────────
#
# Split from a single note_text() into identity vs. content: a note's body is
# ~500 words of technical vocabulary that numerically dominates a single dense
# vector, diluting the project-identity signal that title+tags carry cleanly.
# Measured: weekly-cuisine notes with the project explicit in title/tags scored
# 0.35-0.64 against Projects/WeeklyCuisine, several below the placement threshold.
# suggest_folder() embeds both separately and weight-combines the two cosine
# similarities per folder — see decision/note-tags-should-feed-the-placement-
# embedding-measured-signal-dilution and the fortalecer-dominios-de-conocimiento
# spec in the vault for the full measurement.

def note_identity_text(path: Path) -> str:
    """Identity signal to embed: stem + tags.

    Deliberately excludes body, folder, and wikilinks. Not the same as
    core/ingest.py's _build_context_prefix (used for the search index) — that
    one includes folder and links, both wrong signals here: folder is circular
    (we're trying to determine the folder), and the wikilink graph is saturated
    by auto_link (~12 links per note), not discriminative for domain.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    fm, _body = extract_frontmatter(raw)

    # Lazy import avoids a module-level cycle.
    from archiver_rag.graph.terms import _note_tags

    stem_readable = re.sub(r"[-_]+", " ", path.stem)
    tags = _note_tags(fm)
    return f"{stem_readable}. {' '.join(tags)}".strip() if tags else stem_readable


def note_content_text(path: Path) -> str:
    """Content signal to embed: the note body.

    Frontmatter stripped (meta, not content) and the ## Related section stripped
    (linker.py writes ~12 neighbour slugs per note — those describe other notes,
    not this one). Truncated to ~512 words, comfortable for all-MiniLM-L6-v2's
    256-token window.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    _fm, body = extract_frontmatter(raw)
    body = strip_related_section(body).strip()
    words = body.split()
    if len(words) > 512:
        body = " ".join(words[:512])
    return body


# ──────────────────────────────────────────────────────────────────────────────
# Name-prefix bonus (Fase C, spec fortalecer-dominios) — deterministic complement
# to the semantic signal for notes that literally name their project in the stem
# (weekly-cuisine-phase-3, bakery-api-recipes, archiver-rag-sync-command). High
# precision, low coverage: only a bonus, never a short-circuit, so a misleading
# name can still be overridden by the semantic signal.
# ──────────────────────────────────────────────────────────────────────────────

def _folder_prefix(rel_folder: str) -> str:
    """Normalize a folder's last path segment to a comparable kebab-case prefix.

    'Projects/WeeklyCuisine' -> 'weekly-cuisine', 'Projects/ArchiverRAG' ->
    'archiver-rag' (not 'archiver-r-a-g' — acronym runs like RAG are kept intact
    as one word boundary), 'Projects/PanaderiaFatima/fixes' -> 'fixes' (last
    segment only).
    """
    name = Path(rel_folder).name
    # lowerUpper -> lower-Upper (e.g. weeklyCuisine -> weekly-Cuisine)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    # ACRONYMFollowedByWord -> ACRONYM-FollowedByWord (e.g. ArchiverRAG's "RAG"
    # run stays intact: the boundary goes before the last capital in the run)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", s)
    return s.lower()


def _matches_prefix(stem: str, prefix: str) -> bool:
    """True if stem starts with prefix as a whole segment, not a free substring
    (avoids 'archiver' matching a short prefix inside an unrelated longer stem)."""
    stem_norm = stem.lower()
    return stem_norm == prefix or stem_norm.startswith(prefix + "-")


# ──────────────────────────────────────────────────────────────────────────────
# Placement
# ──────────────────────────────────────────────────────────────────────────────

def suggest_folder(
    vault: Path,
    note_path: Path,
    *,
    threshold: float = 0.55,
    type_fallback: bool = True,
    w_identity: float = 0.6,
    w_content: float = 0.4,
    name_prefix_bonus: float = 0.15,
) -> dict:
    """Semantic placement for a single note.

    Returns::
        {
          "suggested_folder": str | None,   # vault-relative path, e.g. "gotcha"
          "similarity": float,              # cosine of best match (0 if fallback/none)
          "reason": "semantic" | "type" | "none",
          "scores": {rel_folder: similarity, ...},  # all folders, for reporting
        }

    Steps:
    1. Embed identity (stem+tags) and content (body) separately, one batched call.
    2. Weighted cosine (w_identity·cos_identity + w_content·cos_content) against
       every folder centroid — the folder side stays a single embedding of its
       declared description, only the note side is split.
    3. Add name_prefix_bonus to any folder whose normalized name prefixes the
       note's stem (e.g. weekly-cuisine-phase-3 vs. Projects/WeeklyCuisine) —
       additive, applied before picking the winner, so it can flip a close call.
    4. Best above threshold → that folder.
    5. If none and type_fallback → use frontmatter type: field (sanitized).
    6. Otherwise None.

    If the note has no body (content_text empty), falls back to identity alone
    (effective weight 1.0) rather than embedding an empty string.
    """
    from archiver_rag.core.embedder import embed

    centroids = _centroids_mod.folder_centroids(vault)

    identity_text = note_identity_text(note_path)
    content_text = note_content_text(note_path)

    if not identity_text or not centroids:
        folder = _type_folder(note_path) if type_fallback else None
        return {
            "suggested_folder": folder,
            "similarity": 0.0,
            "reason": "type" if folder else "none",
            "scores": {},
        }

    # One batched call regardless of whether content is present.
    texts = [identity_text, content_text] if content_text else [identity_text]
    vecs = embed(texts)
    identity_vec = _centroids_mod._unit(vecs[0])
    content_vec = _centroids_mod._unit(vecs[1]) if content_text else None

    scores: dict[str, float] = {
        rel_folder: _centroids_mod.weighted_cosine(
            [(w_identity, identity_vec, centroid), (w_content, content_vec, centroid)]
        )
        for rel_folder, centroid in centroids.items()
    }

    if name_prefix_bonus:
        # scores' keys are exactly centroids' keys (folder_centroids()'s output) —
        # every candidate here is already "currently described", no separate
        # eligibility lookup needed. No separate "is this a project folder"
        # marker exists on FolderNote: type-folders (decision/gotcha/lesson/
        # pattern/reference) structurally never have a _folder.md post-recovery
        # (see CLAUDE.md, folder-collapse incident), so "currently described"
        # already means "project folder" in this vault. If a type-folder is ever
        # given a description again, it would start receiving the bonus too —
        # revisit then.
        stem = note_path.stem
        for rel_folder in scores:
            if _matches_prefix(stem, _folder_prefix(rel_folder)):
                scores[rel_folder] += name_prefix_bonus

    best_folder, best_score = max(scores.items(), key=lambda kv: kv[1])

    if best_score >= threshold:
        return {
            "suggested_folder": best_folder,
            "similarity": round(best_score, 4),
            "reason": "semantic",
            "scores": {k: round(v, 4) for k, v in scores.items()},
        }

    if type_fallback:
        folder = _type_folder(note_path)
        if folder:
            return {
                "suggested_folder": folder,
                "similarity": round(best_score, 4),
                "reason": "type",
                "scores": {k: round(v, 4) for k, v in scores.items()},
            }

    return {
        "suggested_folder": None,
        "similarity": round(best_score, 4),
        "reason": "none",
        "scores": {k: round(v, 4) for k, v in scores.items()},
    }


def _type_folder(note_path: Path) -> str | None:
    """Return the frontmatter type: value, sanitized as a folder name.

    Strips path separators to prevent traversal — same rule as vault/notes.py::log_note.
    """
    try:
        raw = note_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    fm, _body = extract_frontmatter(raw)
    raw_type = str(fm.get("type", "") or "").strip()
    if not raw_type:
        return None
    # Strip any path separators the same way log_note does
    sanitized = re.sub(r"[/\\]", "", raw_type).strip()
    return sanitized or None
