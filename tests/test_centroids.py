"""Tests for graph/centroids.py — fingerprint-keyed centroid cache."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from archiver_rag.vault.folder_notes import FolderNote, write_folder_note
from archiver_rag.graph.centroids import (
    description_text,
    fingerprint,
    _unit,
    cosine,
    weighted_cosine,
    folder_centroids,
    refresh_centroid,
    drop_centroid,
    _load_cache,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _redirect_cache(monkeypatch, tmp_path: Path) -> Path:
    """Redirect cache writes to a temp dir so real ~/.archiver-rag is never touched."""
    cache_file = tmp_path / "centroids.json"
    monkeypatch.setattr("archiver_rag.graph.centroids._cache_path", lambda: cache_file)
    return cache_file


def _make_sidecar(vault: Path, rel_folder: str, terms: list[str]) -> FolderNote:
    # described_folders() (and therefore folder_centroids()) now requires at least one
    # real note on disk, not just a _folder.md — a folder that emptied out must stop
    # competing as a placement candidate (recovery fix, folder-collapse incident). Write
    # a placeholder real note alongside the sidecar so these cache-behavior tests keep
    # exercising folder_centroids() the way they did before that invariant existed.
    folder_dir = vault / rel_folder
    folder_dir.mkdir(parents=True, exist_ok=True)
    placeholder = folder_dir / "placeholder.md"
    if not placeholder.exists():
        placeholder.write_text("# Placeholder\nContent.", encoding="utf-8")
    note = FolderNote(rel_folder=rel_folder, description_terms=terms, source="auto")
    write_folder_note(vault, note)
    return note


# ──────────────────────────────────────────────────────────────────────────────
# description_text, fingerprint
# ──────────────────────────────────────────────────────────────────────────────

def test_description_text_joins_terms():
    note = FolderNote("gotcha", description_terms=["watcher", "atomic-save"])
    assert description_text(note) == "watcher, atomic-save"


def test_description_text_excludes_distinctive():
    note = FolderNote("gotcha", description_terms=["watcher"], distinctive=["rbac"])
    assert "rbac" not in description_text(note)


def test_description_text_empty_when_no_terms():
    note = FolderNote("gotcha", description_terms=[])
    assert description_text(note) == ""


def test_fingerprint_changes_when_terms_change():
    assert fingerprint("watcher, atomic-save") != fingerprint("watcher, chromadb")


def test_fingerprint_stable_for_same_text():
    t = "watcher, atomic-save"
    assert fingerprint(t) == fingerprint(t)


# ──────────────────────────────────────────────────────────────────────────────
# _unit, cosine
# ──────────────────────────────────────────────────────────────────────────────

def test_unit_normalizes_vector():
    v = _unit([3.0, 4.0])
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6


def test_unit_handles_zero_vector():
    v = _unit([0.0, 0.0, 0.0])
    # Zero vector has norm 0; _unit returns it unchanged (no NaN)
    assert np.all(np.isfinite(v))


def test_cosine_identical_vectors():
    v = _unit([1.0, 2.0, 3.0])
    assert abs(cosine(v, v) - 1.0) < 1e-5


def test_cosine_orthogonal_vectors():
    a = _unit([1.0, 0.0])
    b = _unit([0.0, 1.0])
    assert abs(cosine(a, b)) < 1e-5


# ──────────────────────────────────────────────────────────────────────────────
# weighted_cosine — shared by suggest_folder (note vs. folder centroid) and
# graph/inbox.py (note vs. note), so the weighting/fallback logic lives once.
# ──────────────────────────────────────────────────────────────────────────────

def test_weighted_cosine_two_channels_matches_manual_sum():
    a1, a2 = _unit([1.0, 0.0]), _unit([0.0, 1.0])
    b1, b2 = _unit([1.0, 0.0]), _unit([0.0, 1.0])
    result = weighted_cosine([(0.6, a1, b1), (0.4, a2, b2)])
    expected = 0.6 * cosine(a1, b1) + 0.4 * cosine(a2, b2)
    assert abs(result - expected) < 1e-6


def test_weighted_cosine_missing_channel_renormalizes_to_identity_only():
    """Mirrors suggest_folder's original 'no body -> identity-only, effective
    weight 1.0' fallback: dropping one channel must not silently halve the score."""
    identity = _unit([1.0, 0.0])
    result = weighted_cosine([(0.6, identity, identity), (0.4, None, None)])
    assert abs(result - 1.0) < 1e-6


def test_weighted_cosine_all_channels_missing_returns_zero():
    assert weighted_cosine([(0.6, None, None), (0.4, None, None)]) == 0.0


def test_weighted_cosine_partial_missing_pair_drops_that_channel():
    """A channel with only one side present (e.g. vec_a exists, vec_b is None) is
    dropped just like a fully-missing channel — cosine needs both sides."""
    identity = _unit([1.0, 0.0])
    result = weighted_cosine([(0.6, identity, identity), (0.4, identity, None)])
    assert abs(result - 1.0) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# folder_centroids — cache hit / miss / staleness
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_folder_centroids_returns_unit_vectors(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)
    _make_sidecar(vault, "gotcha", ["watcher", "atomic-save"])

    centroids = folder_centroids(vault)
    assert "gotcha" in centroids
    vec = centroids["gotcha"]
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5


@pytest.mark.slow
def test_folder_centroids_empty_terms_excluded(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)
    _make_sidecar(vault, "empty-folder", [])

    centroids = folder_centroids(vault)
    assert "empty-folder" not in centroids


@pytest.mark.slow
def test_folder_centroids_reuses_cache_on_second_call(tmp_path, monkeypatch):
    """Second call must reuse the stored vector, not re-embed."""
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)
    _make_sidecar(vault, "gotcha", ["watcher"])

    embed_calls = []
    original_embed = None

    def _patched_embed(texts):
        embed_calls.append(texts)
        import importlib
        embedder = importlib.import_module("archiver_rag.core.embedder")
        return embedder._get_model().encode(texts, show_progress_bar=False).tolist()

    monkeypatch.setattr("archiver_rag.core.embedder.embed", _patched_embed)

    folder_centroids(vault)  # first call — embeds
    n_first = len(embed_calls)

    folder_centroids(vault)  # second call — should read from cache
    assert len(embed_calls) == n_first, "re-embedded on second call despite unchanged description"


@pytest.mark.slow
def test_folder_centroids_reembeds_when_terms_change(tmp_path, monkeypatch):
    """Editing description_terms must change the fingerprint and force a new embedding."""
    vault = make_vault(tmp_path)
    cache_file = _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    v1 = folder_centroids(vault)["gotcha"].copy()

    # Simulate editing _folder.md by hand — write new terms
    _make_sidecar(vault, "gotcha", ["completely-different-topic", "chromadb", "cosine"])
    # Don't call refresh_centroid; prove fingerprint-based lookup handles it
    v2 = folder_centroids(vault)["gotcha"].copy()

    assert not np.allclose(v1, v2), "vector did not change after description change"


@pytest.mark.slow
def test_folder_centroids_corrupt_cache_falls_back_to_embed(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    cache_file = _redirect_cache(monkeypatch, tmp_path)
    cache_file.write_text("NOT JSON", encoding="utf-8")

    _make_sidecar(vault, "gotcha", ["watcher"])
    centroids = folder_centroids(vault)
    assert "gotcha" in centroids, "corrupt cache should trigger fallback embed, not error"


@pytest.mark.slow
def test_folder_centroids_stale_entry_removed_from_cache(tmp_path, monkeypatch):
    """Folders that no longer have a sidecar must be evicted from the cache."""
    vault = make_vault(tmp_path)
    cache_file = _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    folder_centroids(vault)  # populate cache

    # Remove the sidecar by deleting the folder note
    (vault / "gotcha" / "_folder.md").unlink()

    folder_centroids(vault)  # should evict gotcha

    cache = _load_cache()
    assert "gotcha" not in cache


# ──────────────────────────────────────────────────────────────────────────────
# refresh_centroid
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_refresh_centroid_returns_true_on_change(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    folder_centroids(vault)  # seed cache

    _make_sidecar(vault, "gotcha", ["completely-different"])
    changed = refresh_centroid(vault, "gotcha")
    assert changed


@pytest.mark.slow
def test_refresh_centroid_returns_false_when_unchanged(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    folder_centroids(vault)  # seed cache

    changed = refresh_centroid(vault, "gotcha")
    assert not changed, "re-embedding an unchanged description should return False"


@pytest.mark.slow
def test_refresh_centroid_empty_terms_removes_entry(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    folder_centroids(vault)

    # Overwrite with empty terms
    _make_sidecar(vault, "gotcha", [])
    changed = refresh_centroid(vault, "gotcha")
    assert changed
    assert "gotcha" not in _load_cache()


# ──────────────────────────────────────────────────────────────────────────────
# drop_centroid
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_drop_centroid_removes_entry(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    folder_centroids(vault)

    result = drop_centroid("gotcha")
    assert result
    assert "gotcha" not in _load_cache()


def test_drop_centroid_returns_false_when_absent(tmp_path, monkeypatch):
    _redirect_cache(monkeypatch, tmp_path)
    assert not drop_centroid("nonexistent-folder")


@pytest.mark.slow
def test_drop_centroid_leaves_other_entries(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    _redirect_cache(monkeypatch, tmp_path)

    _make_sidecar(vault, "gotcha", ["watcher"])
    _make_sidecar(vault, "decision", ["clustering"])
    folder_centroids(vault)

    drop_centroid("gotcha")
    cache = _load_cache()
    assert "decision" in cache
    assert "gotcha" not in cache
