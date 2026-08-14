"""Tests for graph/placement.py — semantic folder placement."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from archiver_rag.graph.placement import note_text, _type_folder, suggest_folder
from archiver_rag.vault.folder_notes import FolderNote, write_folder_note


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def write_note(vault: Path, rel: str, text: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _redirect_cache(monkeypatch, tmp_path: Path) -> Path:
    cache_file = tmp_path / "centroids.json"
    monkeypatch.setattr("archiver_rag.graph.centroids._cache_path", lambda: cache_file)
    return cache_file


# ──────────────────────────────────────────────────────────────────────────────
# note_text
# ──────────────────────────────────────────────────────────────────────────────

def test_note_text_strips_frontmatter(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\ntype: gotcha\n---\nBody here.", encoding="utf-8")
    text = note_text(p)
    assert "type: gotcha" not in text
    assert "Body here" in text


def test_note_text_strips_related_section(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("Body text.\n\n## Related\n- [[foo]]\n- [[bar]]\n", encoding="utf-8")
    text = note_text(p)
    assert "[[foo]]" not in text
    assert "Body text" in text


def test_note_text_includes_stem(tmp_path):
    p = tmp_path / "watcher-atomic-save.md"
    p.write_text("# Watcher\nContent.", encoding="utf-8")
    text = note_text(p)
    # Stem hyphens become spaces
    assert "watcher atomic save" in text.lower()


def test_note_text_missing_file_returns_empty(tmp_path):
    p = tmp_path / "nonexistent.md"
    assert note_text(p) == ""


def test_note_text_includes_tags_list(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        '---\ntype: decision\ntags: ["weekly-cuisine", "sqlite"]\n---\nBody here.',
        encoding="utf-8",
    )
    text = note_text(p)
    assert "weekly-cuisine" in text
    assert "sqlite" in text
    assert "type: decision" not in text
    assert "Body here" in text


def test_note_text_includes_tags_comma_string(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        '---\ntags: "weekly-cuisine, sqlite"\n---\nBody here.',
        encoding="utf-8",
    )
    text = note_text(p)
    assert "weekly-cuisine" in text
    assert "sqlite" in text


def test_note_text_no_tags_field_unchanged(tmp_path):
    p = tmp_path / "watcher-atomic-save.md"
    p.write_text("Content.", encoding="utf-8")
    text = note_text(p)
    assert text == "watcher atomic save. Content."


# ──────────────────────────────────────────────────────────────────────────────
# _type_folder
# ──────────────────────────────────────────────────────────────────────────────

def test_type_folder_reads_frontmatter_type(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\ntype: gotcha\n---\n", encoding="utf-8")
    assert _type_folder(p) == "gotcha"


def test_type_folder_strips_path_separators(tmp_path):
    """Traversal guard: type: '../evil' must not become a parent-traversal path."""
    p = tmp_path / "note.md"
    p.write_text("---\ntype: ../evil\n---\n", encoding="utf-8")
    result = _type_folder(p)
    assert "/" not in (result or "")
    assert "\\" not in (result or "")


def test_type_folder_returns_none_when_absent(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# No frontmatter\n", encoding="utf-8")
    assert _type_folder(p) is None


# ──────────────────────────────────────────────────────────────────────────────
# suggest_folder — stubbed centroids (fast, no model)
# ──────────────────────────────────────────────────────────────────────────────

def _make_centroid_stub(monkeypatch, centroids: dict):
    """Make folder_centroids return pre-computed unit vectors from a dict of names."""
    import numpy as np
    vecs = {}
    for i, (name, _desc) in enumerate(centroids.items()):
        v = np.zeros(3, dtype=np.float32)
        v[i % 3] = 1.0
        vecs[name] = v
    monkeypatch.setattr("archiver_rag.graph.placement.folder_centroids", lambda vault: vecs)
    return vecs


def test_suggest_folder_returns_best_above_threshold(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import numpy as np

    gotcha_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    decision_vec = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    import archiver_rag.graph.placement as _pm
    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids", lambda v: {"gotcha": gotcha_vec, "decision": decision_vec})
    # Note embedding very similar to gotcha
    note_vec = np.array([0.95, 0.1, 0.0], dtype=np.float32)
    note_vec = note_vec / np.linalg.norm(note_vec)
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [note_vec.tolist()],
    )

    note_path = write_note(vault, "gotcha/my-note.md", "# Note\nSome content.")
    result = suggest_folder(vault, note_path, threshold=0.5, type_fallback=False)

    assert result["suggested_folder"] == "gotcha"
    assert result["reason"] == "semantic"
    assert result["similarity"] > 0.5


def test_suggest_folder_below_threshold_uses_type_fallback(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import numpy as np
    import archiver_rag.graph.placement as _pm

    low_sim_vec = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids",
        lambda v: {"gotcha": np.array([1.0, 0.0, 0.0], dtype=np.float32)})
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [low_sim_vec.tolist()],
    )

    note_path = write_note(
        vault, "misc/my-note.md", "---\ntype: gotcha\n---\nContent."
    )
    result = suggest_folder(vault, note_path, threshold=0.9, type_fallback=True)

    assert result["suggested_folder"] == "gotcha"
    assert result["reason"] == "type"


def test_suggest_folder_type_fallback_disabled_returns_none(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import numpy as np
    import archiver_rag.graph.placement as _pm

    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids",
        lambda v: {"gotcha": np.array([1.0, 0.0, 0.0], dtype=np.float32)})
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [np.array([0.0, 0.0, 1.0], dtype=np.float32).tolist()],
    )

    note_path = write_note(
        vault, "misc/my-note.md", "---\ntype: gotcha\n---\nContent."
    )
    result = suggest_folder(vault, note_path, threshold=0.99, type_fallback=False)

    assert result["suggested_folder"] is None
    assert result["reason"] == "none"


def test_suggest_folder_no_descriptions_falls_back_to_type(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import archiver_rag.graph.placement as _pm
    # No described folders at all
    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids", lambda v: {})

    note_path = write_note(
        vault, "gotcha/my-note.md", "---\ntype: decision\n---\nContent."
    )
    result = suggest_folder(vault, note_path, type_fallback=True)
    assert result["suggested_folder"] == "decision"
    assert result["reason"] == "type"
    assert result["scores"] == {}


def test_suggest_folder_scores_returned(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import numpy as np
    import archiver_rag.graph.placement as _pm

    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids", lambda v: {
        "gotcha": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "decision": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    })
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [np.array([0.8, 0.2, 0.0], dtype=np.float32).tolist()],
    )

    note_path = write_note(vault, "misc/my-note.md", "# Note\nContent.")
    result = suggest_folder(vault, note_path, threshold=0.5)

    assert "gotcha" in result["scores"]
    assert "decision" in result["scores"]
    assert result["scores"]["gotcha"] > result["scores"]["decision"]
