"""
Tests for VaultHandler._maybe_cluster.

Observed live before these existed: a single new note logged "Auto-placed … → decision/"
five times. cluster_note kept suggesting the folder the note was already in, so the
handler re-issued a move onto the note's own path. move_notes rejected each one, but the
handler logged success anyway and every attempt re-triggered ingest + auto_link.

Stage B: placement is now by cosine similarity (suggest_folder) not neighbour vote.
Tests mock suggest_folder; the anti-churn guard now compares full vault-relative parent
paths rather than just the immediate directory name.

Recovery fix (folder collapse incident): the cluster_vault() label-propagation fallback
that used to fire automatically after `cluster_threshold` no-suggestion notes in a row
has been removed from _maybe_cluster entirely — it collapsed 61/73 real vault notes into
two note-stem-named folders across a handful of automatic re-cluster passes.
cluster_vault/apply_clusters are no longer imported by watcher.py at all; they remain
reachable only via the explicit manual `archiver-rag cluster` CLI command.
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
    calls = {"moves": [], "logged": []}

    # Stage B: _get_cluster_config now returns 4-tuple: (auto_cluster, threshold, sim_threshold, type_fallback)
    monkeypatch.setattr("archiver_rag.watcher._get_cluster_config", lambda: (True, 5, 0.55, True))
    monkeypatch.setattr(
        "archiver_rag.watcher._log", lambda m: calls["logged"].append(m)
    )

    def _move_notes(moves):
        calls["moves"].append(moves)
        return {"moved": len(moves), "failed": 0, "succeeded": moves, "errors": []}

    monkeypatch.setattr("archiver_rag.vault.reorganize.move_notes", _move_notes)
    return calls


def _suggest(monkeypatch, folder, reason="semantic", similarity=0.72):
    """Patch suggest_folder to return a canned result."""
    def _fake_suggest_folder(vault, note_path, *, threshold=0.55, type_fallback=True, w_identity=0.6, w_content=0.4, name_prefix_bonus=0.15):
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


# ── Gate 1: newly created destination folders get a real description ────────


def test_new_destination_folder_gets_real_description_not_empty_placeholder(
    tmp_vault, monkeypatch
):
    """A note auto-placed into a brand-new folder used to leave it with
    description_terms=[] (an empty placeholder), which folder_centroids() then skips
    entirely until auto_describe (if even on) eventually filled it in. Gate 1: extract
    real tag-based terms from the note that just landed there instead."""
    monkeypatch.setattr("archiver_rag.watcher._get_cluster_config", lambda: (True, 5, 0.55, True))
    monkeypatch.setattr("archiver_rag.watcher._log", lambda m: None)
    _suggest(monkeypatch, "new-topic", reason="semantic", similarity=0.7)

    note = tmp_vault.write(
        "loose.md", "---\ntags: [watcher, clustering]\n---\n# Loose\nBody."
    )

    def _move_notes(moves):
        # Simulate the real move so post-move term extraction has a file to read —
        # unlike cluster_spy's generic fake, which never touches disk.
        for m in moves:
            src = Path(tmp_vault.root) / m["source"]
            dest = Path(tmp_vault.root) / m["destination"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
        return {"moved": len(moves), "failed": 0, "succeeded": moves, "errors": []}

    monkeypatch.setattr("archiver_rag.vault.reorganize.move_notes", _move_notes)

    VaultHandler()._maybe_cluster(str(note))

    from archiver_rag.vault.folder_notes import read_folder_note

    sidecar = read_folder_note(Path(tmp_vault.root), "new-topic")
    assert sidecar is not None
    assert sidecar.description_terms, "must be a real description, not an empty placeholder"


def test_no_suggestion_never_triggers_cluster_vault_fallback(tmp_vault, cluster_spy, monkeypatch):
    """Recovery fix: repeated no-suggestion notes must never fall back to
    cluster_vault()/apply_clusters() — that automatic path caused the folder
    collapse and has been removed. cluster_vault must not even be imported by
    watcher.py; patching it here would silently pass if it were reintroduced,
    so assert directly on the module instead."""
    import archiver_rag.watcher as w

    assert not hasattr(w, "cluster_vault"), "cluster_vault must not be imported into watcher.py"
    assert not hasattr(w, "apply_clusters"), "apply_clusters must not be imported into watcher.py"

    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    for _ in range(10):
        result = VaultHandler()._maybe_cluster(str(note))
        assert result is None
    assert cluster_spy["moves"] == [], "no-suggestion notes must never trigger a move"


# ── Gate 2: inbox routing (reason == "none") ─────────────────────────────────
# auto_inbox ships off by default — see AGENTS.md Pending Work. These tests cover
# the watcher-level wiring in isolation; graph/inbox.py's own grouping/naming/
# spin-out logic is covered by tests/test_inbox_clustering.py.


def test_inbox_routing_disabled_by_default_leaves_note_in_place(
    tmp_vault, cluster_spy, monkeypatch
):
    """auto_inbox defaults to False — a reason=='none' note must be left exactly
    where it is, same as before Gate 2 existed (this is also covered by the
    fail-safe-config test below, but pinned here at the _maybe_cluster level too)."""
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (False, 3, 0.5))
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    result = VaultHandler()._maybe_cluster(str(note))

    assert result is None
    assert cluster_spy["moves"] == []


def test_inbox_routing_moves_note_when_enabled(tmp_vault, cluster_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (True, 3, 0.5))
    monkeypatch.setattr("archiver_rag.graph.inbox.maybe_spin_out_clusters", lambda *a, **kw: [])
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    result = VaultHandler()._maybe_cluster(str(note))

    assert result == "inbox"
    assert cluster_spy["moves"] == [
        [{"source": "lonely.md", "destination": "inbox/lonely.md"}]
    ]
    assert any("inbox" in m for m in cluster_spy["logged"])


def test_inbox_routing_writes_locked_folder_note_once(tmp_vault, cluster_spy, monkeypatch):
    """inbox/_folder.md must be written as source: manual with empty terms the first
    time a note lands there — this is what keeps it out of placement candidacy and
    immune to auto-description (see _ensure_inbox_locked)."""
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (True, 3, 0.5))
    monkeypatch.setattr("archiver_rag.graph.inbox.maybe_spin_out_clusters", lambda *a, **kw: [])
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    VaultHandler()._maybe_cluster(str(note))

    from archiver_rag.vault.folder_notes import read_folder_note

    sidecar = read_folder_note(Path(tmp_vault.root), "inbox")
    assert sidecar is not None
    assert sidecar.source == "manual"
    assert sidecar.description_terms == []


def test_inbox_folder_note_never_overwritten_by_redescribe(tmp_vault, monkeypatch):
    """Even with auto_describe on, _maybe_redescribe('inbox') must be a no-op —
    apply_extracted_terms already refuses to touch a source: manual folder, and
    that's the entire mechanism keeping inbox/ out of placement. No new guard code
    should exist in _maybe_redescribe itself for this."""
    monkeypatch.setattr("archiver_rag.watcher._last_redescribed", {})

    from archiver_rag.watcher import _ensure_inbox_locked, _maybe_redescribe
    from archiver_rag.vault.folder_notes import read_folder_note

    vault = Path(tmp_vault.root)
    tmp_vault.write("inbox/lonely.md", "---\ntags: [something]\n---\n# Lonely")
    _ensure_inbox_locked(vault)

    monkeypatch.setattr(
        "archiver_rag.watcher._get_describe_config",
        lambda: (True, 4, 6, 0.5, 1.0, True),
    )
    _maybe_redescribe("inbox")

    sidecar = read_folder_note(vault, "inbox")
    assert sidecar.source == "manual"
    assert sidecar.description_terms == []


def test_note_already_in_inbox_is_not_re_routed(tmp_vault, cluster_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (True, 3, 0.5))
    _suggest(monkeypatch, None)
    note = tmp_vault.write("inbox/already-there.md", "# Here")

    result = VaultHandler()._maybe_cluster(str(note))

    assert result is None
    assert cluster_spy["moves"] == []


def test_inbox_spin_out_promotes_triggering_note_to_new_folder(
    tmp_vault, cluster_spy, monkeypatch
):
    """If maybe_spin_out_clusters reports the triggering note was itself promoted out
    of inbox in the same pass, _maybe_cluster must return that new folder — mirroring
    the existing 'return the note's final resting folder' contract from Gate 1."""
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (True, 3, 0.5))
    monkeypatch.setattr(
        "archiver_rag.graph.inbox.maybe_spin_out_clusters",
        lambda *a, **kw: [{"folder": "new-topic", "notes": ["inbox/lonely.md"]}],
    )
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    result = VaultHandler()._maybe_cluster(str(note))

    assert result == "new-topic"


def test_inbox_spin_out_unrelated_group_returns_inbox(tmp_vault, cluster_spy, monkeypatch):
    """A spin-out that promotes OTHER notes but not the one that triggered this event
    must still report the triggering note's own resting folder — inbox."""
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (True, 3, 0.5))
    monkeypatch.setattr(
        "archiver_rag.graph.inbox.maybe_spin_out_clusters",
        lambda *a, **kw: [
            {
                "folder": "other-topic",
                "notes": ["inbox/other-a.md", "inbox/other-b.md", "inbox/other-c.md"],
            }
        ],
    )
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")

    result = VaultHandler()._maybe_cluster(str(note))

    assert result == "inbox"


def test_inbox_end_to_end_promotes_triggering_note_via_real_spin_out(
    tmp_vault, monkeypatch
):
    """Exercises the real graph.inbox.maybe_spin_out_clusters (not mocked) to catch
    mismatches at the seam between watcher.py and graph/inbox.py — specifically, a
    pre-move vs. post-move path mismatch in the 'was my note promoted' check that unit
    tests mocking maybe_spin_out_clusters entirely would never expose."""
    monkeypatch.setattr(
        "archiver_rag.watcher._get_cluster_config", lambda: (True, 5, 0.55, True)
    )
    monkeypatch.setattr("archiver_rag.watcher._get_inbox_config", lambda: (True, 3, 0.5))
    monkeypatch.setattr("archiver_rag.watcher._log", lambda m: None)
    monkeypatch.setattr(
        "archiver_rag.core.ingest.prune_orphans", lambda *a, **kw: 0, raising=False
    )
    _suggest(monkeypatch, None)

    def _real_move(moves):
        for m in moves:
            src = Path(tmp_vault.root) / m["source"]
            dest = Path(tmp_vault.root) / m["destination"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
        return {"moved": len(moves), "failed": 0, "succeeded": moves, "errors": []}

    monkeypatch.setattr("archiver_rag.vault.reorganize.move_notes", _real_move)
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed",
        lambda texts: [[1.0, 0.0] if "groupa" in t.lower() else [0.5, 0.5] for t in texts],
    )

    # Two notes already sitting in inbox/ from earlier events.
    tmp_vault.write("inbox/alpha-one.md", '---\ntags: ["groupa"]\n---\nAbout groupa.')
    tmp_vault.write("inbox/alpha-two.md", '---\ntags: ["groupa"]\n---\nAbout groupa.')

    # The note that triggers this event — landing in inbox/ completes the cluster.
    note = tmp_vault.write("alpha-three.md", '---\ntags: ["groupa"]\n---\nAbout groupa.')

    result = VaultHandler()._maybe_cluster(str(note))

    assert result not in (None, "inbox"), (
        "the note that completed the cluster must be reported at its real final "
        "folder, not left at inbox — a path-format mismatch between watcher.py and "
        "graph/inbox.py would silently return 'inbox' here instead"
    )
    assert (Path(tmp_vault.root) / result / "alpha-three.md").exists()
    assert not (Path(tmp_vault.root) / "inbox" / "alpha-three.md").exists()


# ── _get_inbox_config fail-safe defaults ─────────────────────────────────────


def test_get_inbox_config_defaults_on_missing_config():
    from archiver_rag.watcher import _get_inbox_config

    assert _get_inbox_config() == (False, 3, 0.5)
