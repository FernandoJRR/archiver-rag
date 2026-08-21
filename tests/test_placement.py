"""Tests for graph/placement.py — semantic folder placement."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from archiver_rag.graph.placement import (
    note_identity_text,
    note_content_text,
    _type_folder,
    suggest_folder,
    _folder_prefix,
    _matches_prefix,
)
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
# note_identity_text — stem + tags, no body (Fase B, spec fortalecer-dominios)
# ──────────────────────────────────────────────────────────────────────────────

def test_identity_text_strips_frontmatter_and_excludes_body(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\ntype: gotcha\n---\nBody here.", encoding="utf-8")
    text = note_identity_text(p)
    assert "type: gotcha" not in text
    assert "Body here" not in text, "identity text must not include the body"


def test_identity_text_includes_stem(tmp_path):
    p = tmp_path / "watcher-atomic-save.md"
    p.write_text("# Watcher\nContent.", encoding="utf-8")
    text = note_identity_text(p)
    assert "watcher atomic save" in text.lower()


def test_identity_text_missing_file_returns_empty(tmp_path):
    p = tmp_path / "nonexistent.md"
    assert note_identity_text(p) == ""


def test_identity_text_includes_tags_list(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        '---\ntype: decision\ntags: ["weekly-cuisine", "sqlite"]\n---\nBody here.',
        encoding="utf-8",
    )
    text = note_identity_text(p)
    assert "weekly-cuisine" in text
    assert "sqlite" in text
    assert "type: decision" not in text


def test_identity_text_includes_tags_comma_string(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        '---\ntags: "weekly-cuisine, sqlite"\n---\nBody here.',
        encoding="utf-8",
    )
    text = note_identity_text(p)
    assert "weekly-cuisine" in text
    assert "sqlite" in text


def test_identity_text_no_tags_field_unchanged(tmp_path):
    p = tmp_path / "watcher-atomic-save.md"
    p.write_text("Content.", encoding="utf-8")
    assert note_identity_text(p) == "watcher atomic save"


# ──────────────────────────────────────────────────────────────────────────────
# note_content_text — body only, no stem/tags
# ──────────────────────────────────────────────────────────────────────────────

def test_content_text_strips_frontmatter(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\ntype: gotcha\ntags: [x]\n---\nBody here.", encoding="utf-8")
    text = note_content_text(p)
    assert "type: gotcha" not in text
    assert "Body here" in text


def test_content_text_strips_related_section(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("Body text.\n\n## Related\n- [[foo]]\n- [[bar]]\n", encoding="utf-8")
    text = note_content_text(p)
    assert "[[foo]]" not in text
    assert "Body text" in text


def test_content_text_excludes_stem_and_tags(tmp_path):
    p = tmp_path / "watcher-atomic-save.md"
    p.write_text('---\ntags: [watcher]\n---\nJust the body.', encoding="utf-8")
    text = note_content_text(p)
    assert "watcher atomic save" not in text.lower()
    assert "watcher" not in text.lower(), "the tag must not leak into content text"
    assert "Just the body" in text


def test_content_text_missing_file_returns_empty(tmp_path):
    p = tmp_path / "nonexistent.md"
    assert note_content_text(p) == ""


def test_content_text_empty_body_returns_empty(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\ntype: gotcha\n---\n", encoding="utf-8")
    assert note_content_text(p) == ""


def test_content_text_truncates_long_body(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(" ".join(f"word{i}" for i in range(600)), encoding="utf-8")
    text = note_content_text(p)
    assert len(text.split()) == 512


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
        lambda texts: [note_vec.tolist()] * len(texts),
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
        lambda texts: [low_sim_vec.tolist()] * len(texts),
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
        lambda texts: [np.array([0.0, 0.0, 1.0], dtype=np.float32).tolist()] * len(texts),
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
        lambda texts: [np.array([0.8, 0.2, 0.0], dtype=np.float32).tolist()] * len(texts),
    )

    note_path = write_note(vault, "misc/my-note.md", "# Note\nContent.")
    result = suggest_folder(vault, note_path, threshold=0.5)

    assert "gotcha" in result["scores"]
    assert "decision" in result["scores"]
    assert result["scores"]["gotcha"] > result["scores"]["decision"]


# ──────────────────────────────────────────────────────────────────────────────
# suggest_folder — identity/content weighting (Fase B, spec fortalecer-dominios)
# ──────────────────────────────────────────────────────────────────────────────

def test_suggest_folder_weights_identity_and_content_separately(tmp_path, monkeypatch):
    """identity_text and content_text must be embedded and scored independently —
    pointing them at different folders and checking the weighted sum matches the
    w_identity/w_content formula, not just 'some average'."""
    vault = make_vault(tmp_path)
    import numpy as np
    import archiver_rag.graph.placement as _pm

    gotcha_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    decision_vec = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    monkeypatch.setattr(
        _pm._centroids_mod, "folder_centroids",
        lambda v: {"gotcha": gotcha_vec, "decision": decision_vec},
    )

    identity_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)  # points at gotcha
    content_vec = np.array([0.0, 1.0, 0.0], dtype=np.float32)   # points at decision

    def _fake_embed(texts):
        # suggest_folder always passes [identity_text, content_text] when both exist
        return [identity_vec.tolist(), content_vec.tolist()]

    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed)

    note_path = write_note(vault, "misc/my-note.md", "# Note\nSome content here.")
    result = suggest_folder(
        vault, note_path, threshold=0.0, type_fallback=False,
        w_identity=0.7, w_content=0.3,
    )

    # gotcha: 0.7*cos(identity, gotcha) + 0.3*cos(content, gotcha) = 0.7*1 + 0.3*0 = 0.7
    # decision: 0.7*cos(identity, decision) + 0.3*cos(content, decision) = 0.7*0 + 0.3*1 = 0.3
    assert result["scores"]["gotcha"] == pytest.approx(0.7, abs=1e-4)
    assert result["scores"]["decision"] == pytest.approx(0.3, abs=1e-4)
    assert result["suggested_folder"] == "gotcha"


def test_suggest_folder_falls_back_to_identity_only_when_body_empty(tmp_path, monkeypatch):
    """A note with no body must not crash embedding an empty string — it should
    use identity alone (effective weight 1.0), and embed() must be called with
    a single text, not two."""
    vault = make_vault(tmp_path)
    import numpy as np
    import archiver_rag.graph.placement as _pm

    monkeypatch.setattr(
        _pm._centroids_mod, "folder_centroids",
        lambda v: {"gotcha": np.array([1.0, 0.0, 0.0], dtype=np.float32)},
    )

    calls = []

    def _fake_embed(texts):
        calls.append(list(texts))
        return [np.array([1.0, 0.0, 0.0], dtype=np.float32).tolist()] * len(texts)

    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed)

    note_path = write_note(vault, "misc/my-note.md", "---\ntype: gotcha\n---\n")
    result = suggest_folder(vault, note_path, threshold=0.5, type_fallback=False)

    assert len(calls) == 1 and len(calls[0]) == 1, "empty body must not be embedded"
    assert result["suggested_folder"] == "gotcha"
    assert result["reason"] == "semantic"


# ──────────────────────────────────────────────────────────────────────────────
# _folder_prefix — CamelCase -> kebab-case, acronym-aware (Fase C)
# ──────────────────────────────────────────────────────────────────────────────

def test_folder_prefix_camel_case():
    assert _folder_prefix("Projects/WeeklyCuisine") == "weekly-cuisine"


def test_folder_prefix_acronym_run_stays_intact():
    """ArchiverRAG must become 'archiver-rag', not 'archiver-r-a-g' —
    the boundary goes before the last capital in a run of capitals."""
    assert _folder_prefix("Projects/ArchiverRAG") == "archiver-rag"


def test_folder_prefix_multi_word():
    assert _folder_prefix("Projects/PanaderiaFatima") == "panaderia-fatima"


def test_folder_prefix_nested_uses_last_segment_only():
    assert _folder_prefix("Projects/PanaderiaFatima/fixes") == "fixes"


def test_folder_prefix_already_lowercase():
    assert _folder_prefix("bakery-api-overview") == "bakery-api-overview"


# ──────────────────────────────────────────────────────────────────────────────
# _matches_prefix — whole-segment match, not free substring
# ──────────────────────────────────────────────────────────────────────────────

def test_matches_prefix_exact():
    assert _matches_prefix("weekly-cuisine", "weekly-cuisine") is True


def test_matches_prefix_followed_by_hyphen():
    assert _matches_prefix("weekly-cuisine-phase-3", "weekly-cuisine") is True


def test_matches_prefix_rejects_free_substring():
    """'archiver' must not match inside an unrelated longer stem like
    'archiverama-notes' — only a full segment boundary counts."""
    assert _matches_prefix("archiverama-notes", "archiver") is False


def test_matches_prefix_rejects_unrelated_stem():
    assert _matches_prefix("bakery-api-recipes", "weekly-cuisine") is False


# ──────────────────────────────────────────────────────────────────────────────
# suggest_folder — name-prefix bonus (Fase C, spec fortalecer-dominios)
# ──────────────────────────────────────────────────────────────────────────────

def test_prefix_bonus_flips_a_close_call(tmp_path, monkeypatch):
    """A borderline semantic score plus the prefix bonus must be able to change
    which folder wins — additive, applied before best_folder is chosen."""
    vault = make_vault(tmp_path)
    import archiver_rag.graph.placement as _pm

    # WeeklyCuisine scores lower semantically, but the note's stem is prefixed.
    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids", lambda v: {
        "Projects/WeeklyCuisine": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "bakery-api-overview": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    })
    # identity/content both point slightly more toward bakery-api-overview —
    # cosine gap here is ~0.074, comfortably under the 0.15 bonus.
    vec = np.array([0.45, 0.5, 0.0], dtype=np.float32)
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [vec.tolist()] * len(texts),
    )

    note_path = write_note(vault, "misc/weekly-cuisine-phase-9.md", "# Note\nSome content.")
    result_without_bonus = suggest_folder(
        vault, note_path, threshold=0.0, type_fallback=False, name_prefix_bonus=0.0
    )
    result_with_bonus = suggest_folder(
        vault, note_path, threshold=0.0, type_fallback=False, name_prefix_bonus=0.15
    )

    assert result_without_bonus["suggested_folder"] == "bakery-api-overview"
    assert result_with_bonus["suggested_folder"] == "Projects/WeeklyCuisine"
    assert result_with_bonus["scores"]["Projects/WeeklyCuisine"] == pytest.approx(
        result_without_bonus["scores"]["Projects/WeeklyCuisine"] + 0.15, abs=1e-4
    )


def test_prefix_bonus_does_not_affect_non_matching_folders(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import archiver_rag.graph.placement as _pm

    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids", lambda v: {
        "Projects/WeeklyCuisine": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "bakery-api-overview": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    })
    vec = np.array([0.4, 0.5, 0.0], dtype=np.float32)
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [vec.tolist()] * len(texts),
    )

    note_path = write_note(vault, "misc/weekly-cuisine-phase-9.md", "# Note\nContent.")
    result = suggest_folder(vault, note_path, threshold=0.0, type_fallback=False, name_prefix_bonus=0.15)

    unbonused = suggest_folder(vault, note_path, threshold=0.0, type_fallback=False, name_prefix_bonus=0.0)
    assert result["scores"]["bakery-api-overview"] == pytest.approx(
        unbonused["scores"]["bakery-api-overview"], abs=1e-4
    )


def test_prefix_bonus_zero_is_a_noop(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    import archiver_rag.graph.placement as _pm

    monkeypatch.setattr(_pm._centroids_mod, "folder_centroids", lambda v: {
        "Projects/WeeklyCuisine": np.array([1.0, 0.0, 0.0], dtype=np.float32),
    })
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [vec.tolist()] * len(texts),
    )

    note_path = write_note(vault, "misc/weekly-cuisine-phase-9.md", "# Note\nContent.")
    result = suggest_folder(vault, note_path, threshold=0.5, type_fallback=False, name_prefix_bonus=0.0)
    assert result["scores"]["Projects/WeeklyCuisine"] == pytest.approx(1.0, abs=1e-4)
