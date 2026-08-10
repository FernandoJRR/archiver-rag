"""
Tests for VaultHandler._maybe_cluster.

Observed live before these existed: a single new note logged "Auto-placed … → decision/"
five times. cluster_note kept suggesting the folder the note was already in, so the
handler re-issued a move onto the note's own path. move_notes rejected each one, but the
handler logged success anyway and every attempt re-triggered ingest + auto_link.

Stage B: placement is now by cosine similarity (suggest_folder) not neighbour vote.
Tests mock suggest_folder; the anti-churn guard now compares full vault-relative parent
paths rather than just the immediate directory name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archiver_rag.watcher import VaultHandler


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def cluster_spy(monkeypatch):
    calls = {"moves": [], "logged": [], "full_cluster": 0}

    # Stage B: _get_cluster_config now returns 4-tuple: (auto_cluster, threshold, sim_threshold, type_fallback)
    monkeypatch.setattr("archiver_rag.watcher._get_cluster_config", lambda: (True, 5, 0.55, True))
    monkeypatch.setattr(
        "archiver_rag.watcher._log", lambda m: calls["logged"].append(m)
    )

    def _move_notes(moves):
        calls["moves"].append(moves)
        return {"moved": len(moves), "failed": 0, "succeeded": moves, "errors": []}

    monkeypatch.setattr("archiver_rag.vault.reorganize.move_notes", _move_notes)

    def _cluster_vault(min_cluster_size=2):
        calls["full_cluster"] += 1
        return {"clusters": [], "total_clusters": 0}

    monkeypatch.setattr("archiver_rag.graph.clustering.cluster_vault", _cluster_vault)
    monkeypatch.setattr("archiver_rag.graph.clustering.apply_clusters", lambda c: [])
    return calls


def _suggest(monkeypatch, folder, reason="semantic", similarity=0.72):
    """Patch suggest_folder to return a canned result."""
    def _fake_suggest_folder(vault, note_path, *, threshold=0.55, type_fallback=True):
        return {
            "suggested_folder": folder,
            "similarity": similarity if folder else 0.0,
            "reason": reason if folder else "none",
            "scores": {folder: similarity} if folder else {},
        }
    monkeypatch.setattr("archiver_rag.graph.placement.suggest_folder", _fake_suggest_folder)


# ── anti-churn ────────────────────────────────────────────────────────────────


def test_note_already_in_target_folder_is_not_moved(
    tmp_vault, cluster_spy, monkeypatch
):
    _suggest(monkeypatch, "decision")
    note = tmp_vault.write("decision/already-there.md", "# Here")
    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["moves"] == [], "re-issued a move onto the note's own folder"
    assert cluster_spy["logged"] == [], "logged Auto-placed without moving anything"


def test_note_in_wrong_folder_is_moved(tmp_vault, cluster_spy, monkeypatch):
    _suggest(monkeypatch, "decision")
    note = tmp_vault.write("misc-notes.md", "# Loose")
    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["moves"] == [
        [{"source": "misc-notes.md", "destination": "decision/misc-notes.md"}]
    ]
    assert any("Auto-placed" in m for m in cluster_spy["logged"])


def test_auto_placed_log_includes_reason_and_similarity(tmp_vault, cluster_spy, monkeypatch):
    """Stage B log line must show reason and similarity score."""
    _suggest(monkeypatch, "gotcha", reason="semantic", similarity=0.61)
    note = tmp_vault.write("new-note.md", "# New")
    VaultHandler()._maybe_cluster(str(note))
    assert any("semantic" in m and "0.61" in m for m in cluster_spy["logged"])


def test_failed_move_is_not_logged_as_placed(tmp_vault, cluster_spy, monkeypatch):
    """move_notes reporting zero moves must not produce an 'Auto-placed' line."""
    _suggest(monkeypatch, "decision")
    monkeypatch.setattr(
        "archiver_rag.vault.reorganize.move_notes",
        lambda moves: {
            "moved": 0,
            "failed": 1,
            "succeeded": [],
            "errors": [{"error": "nope"}],
        },
    )
    note = tmp_vault.write("loose.md", "# Loose")
    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["logged"] == []


# ── widened anti-churn guard (full vault-relative path) ───────────────────────


def test_anti_churn_uses_full_relative_parent(tmp_vault, cluster_spy, monkeypatch):
    """A note in 'a/b' with target 'a/b' must not be moved, but target 'b' must not
    collide with the 'a/b' check (different paths)."""
    _suggest(monkeypatch, "sub/gotcha")
    note = tmp_vault.write("sub/gotcha/already-there.md", "# Here")
    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["moves"] == [], "note already in target sub-folder but was moved"


def test_anti_churn_different_subfolder_allows_move(tmp_vault, cluster_spy, monkeypatch):
    """A note in 'other' whose target is 'gotcha' must be moved even though both
    share the same last path component as an unrelated folder."""
    _suggest(monkeypatch, "gotcha")
    note = tmp_vault.write("other/note.md", "# Note")
    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["moves"] == [
        [{"source": "other/note.md", "destination": "gotcha/note.md"}]
    ]


# ── disabled / no-suggestion paths ───────────────────────────────────────────


def test_disabled_auto_cluster_does_nothing(tmp_vault, cluster_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_cluster_config", lambda: (False, 5, 0.55, True))
    _suggest(monkeypatch, "decision")
    note = tmp_vault.write("loose.md", "# Loose")
    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["moves"] == []


def test_no_suggestion_counts_toward_threshold(tmp_vault, cluster_spy, monkeypatch):
    """With no folder suggestion the note increments the counter; the full
    re-cluster fires only once the threshold is reached."""
    import archiver_rag.watcher as w

    monkeypatch.setattr(w, "_new_notes_since_cluster", 0)
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    for _ in range(4):
        VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["full_cluster"] == 0, (
        "re-clustered before reaching the threshold"
    )

    VaultHandler()._maybe_cluster(str(note))
    assert cluster_spy["full_cluster"] == 1
