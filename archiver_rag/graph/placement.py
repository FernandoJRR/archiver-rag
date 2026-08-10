"""Semantic folder placement for a note — Stage B of spec §3.

replace the wikilink-neighbour vote (`cluster_note`) with cosine similarity against
declared folder descriptions (`folder_centroids`). This signal has no rich-get-richer
term: a folder does not become more similar to a note just by holding more notes.

vault: Path is always explicit, get_vault_path() never bound at module level.
"""

from __future__ import annotations

import re
from pathlib import Path

from archiver_rag.utils import extract_frontmatter
from archiver_rag.graph import centroids as _centroids_mod


# ──────────────────────────────────────────────────────────────────────────────
# Note text to embed
# ──────────────────────────────────────────────────────────────────────────────

def note_text(path: Path) -> str:
    """Build the embedding text for a note.

    Strips frontmatter (meta, not content) and the ## Related section (linker.py
    writes ~12 neighbour slugs per note — those describe other notes, not this one).
    Prepends the stem with spaces instead of hyphens because the stem is identity
    and carries real signal, but dense hyphens look like noise to the model.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    _fm, body = extract_frontmatter(raw)

    # Strip ## Related section — lazy import avoids a module-level cycle.
    from archiver_rag.graph.terms import _strip_related_section
    body = _strip_related_section(body)

    stem_readable = re.sub(r"[-_]+", " ", path.stem)
    text = f"{stem_readable}. {body}".strip()
    # Truncate at roughly 512 words (comfortable for all-MiniLM-L6-v2's 256-token window)
    words = text.split()
    if len(words) > 512:
        text = " ".join(words[:512])
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Placement
# ──────────────────────────────────────────────────────────────────────────────

def suggest_folder(
    vault: Path,
    note_path: Path,
    *,
    threshold: float = 0.55,
    type_fallback: bool = True,
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
    1. Embed the note.
    2. Cosine against every folder centroid.
    3. Best above threshold → that folder.
    4. If none and type_fallback → use frontmatter type: field (sanitized).
    5. Otherwise None.
    """
    from archiver_rag.core.embedder import embed
    import numpy as np

    centroids = _centroids_mod.folder_centroids(vault)

    text = note_text(note_path)
    if not text or not centroids:
        folder = _type_folder(note_path) if type_fallback else None
        return {
            "suggested_folder": folder,
            "similarity": 0.0,
            "reason": "type" if folder else "none",
            "scores": {},
        }

    # embed returns unnormalized — unit-normalize before dotting
    note_vec = _centroids_mod._unit(embed([text])[0])

    scores: dict[str, float] = {
        rel_folder: _centroids_mod.cosine(note_vec, centroid)
        for rel_folder, centroid in centroids.items()
    }

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
