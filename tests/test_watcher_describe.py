"""Tests for auto-describe: _get_describe_config, _maybe_redescribe, and their wiring
into on_created / on_deleted / on_moved (structural changes only — never on_modified).

auto_describe defaults to False (config absent or unreadable), matching the same
safety contract as _get_cluster_config: uncertainty must never turn on a behavior
that rewrites _folder.md files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archiver_rag.watcher import VaultHandler, _get_describe_config, _maybe_redescribe


# ──────────────────────────────────────────────────────────────────────────────
# _get_describe_config — safe-default config reader
# ──────────────────────────────────────────────────────────────────────────────

def test_describe_config_defaults_off_when_home_has_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    auto_describe, min_notes, max_terms, mmr_lambda, alpha_scale = _get_describe_config()
    assert auto_describe is False
    assert (min_notes, max_terms, mmr_lambda, alpha_scale) == (4, 6, 0.5, 1.0)


def test_describe_config_defaults_off_on_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / ".archiver-rag"
    install.mkdir()
    (install / "config.json").write_text("not json at all }{", encoding="utf-8")
    auto_describe, *_ = _get_describe_config()
    assert auto_describe is False


def test_describe_config_reads_explicit_true(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / ".archiver-rag"
    install.mkdir()
    (install / "config.json").write_text(
        json.dumps({"auto_describe": True, "advanced": {"max_terms": 8}}), encoding="utf-8"
    )
    auto_describe, min_notes, max_terms, mmr_lambda, alpha_scale = _get_describe_config()
    assert auto_describe is True
    assert max_terms == 8


# ──────────────────────────────────────────────────────────────────────────────
# _maybe_redescribe — guards and the happy path
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def describe_spy(monkeypatch):
    # _maybe_redescribe debounces per rel_folder using module-level state that would
    # otherwise leak between tests (several tests here reuse "decision" as rel_folder
    # within the same debounce window) — reset it so each test starts undebounced.
    monkeypatch.setattr("archiver_rag.watcher._last_redescribed", {})

    calls = {"logged": [], "extracted": [], "applied": []}
    monkeypatch.setattr("archiver_rag.watcher._log", lambda m: calls["logged"].append(m))

    def _extract_terms(vault, rel_folder, **kw):
        calls["extracted"].append(rel_folder)
        return (["term-a", "term-b"], ["distinct-a"])

    monkeypatch.setattr("archiver_rag.graph.terms.extract_terms", _extract_terms)
    return calls


def test_disabled_by_default_does_nothing(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (False, 4, 6, 0.5, 1.0))
    tmp_vault.write("decision/one.md", "# One")
    _maybe_redescribe("decision")
    assert describe_spy["extracted"] == []
    assert describe_spy["logged"] == []


def test_root_folder_is_never_described(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    _maybe_redescribe(".")
    assert describe_spy["extracted"] == []


def test_empty_folder_is_skipped(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    (Path(tmp_vault.root) / "decision").mkdir()
    _maybe_redescribe("decision")
    assert describe_spy["extracted"] == []


def test_nonexistent_folder_is_skipped(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    _maybe_redescribe("never-created")
    assert describe_spy["extracted"] == []


def test_enabled_folder_with_notes_is_described_and_written(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    tmp_vault.write("decision/one.md", "# One")

    _maybe_redescribe("decision")

    assert describe_spy["extracted"] == ["decision"]
    assert any("Auto-described" in m for m in describe_spy["logged"])

    from archiver_rag.vault.folder_notes import read_folder_note

    note = read_folder_note(Path(tmp_vault.root), "decision")
    assert note is not None
    assert note.description_terms == ["term-a", "term-b"]


def test_manual_folder_is_not_logged_or_overwritten(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    tmp_vault.write("reference/one.md", "# One")

    from archiver_rag.vault.folder_notes import FolderNote, write_folder_note, read_folder_note

    write_folder_note(Path(tmp_vault.root), FolderNote(rel_folder="reference", description_terms=["api"], source="manual"))

    _maybe_redescribe("reference")

    assert describe_spy["logged"] == [], "manual folders must not be touched or logged"
    note = read_folder_note(Path(tmp_vault.root), "reference")
    assert note.description_terms == ["api"]


# ──────────────────────────────────────────────────────────────────────────────
# Recovery fix: debounce — a batch of moves into the same folder must not
# re-extract/re-write once per note
# ──────────────────────────────────────────────────────────────────────────────

def test_redescribe_is_debounced_within_window(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    tmp_vault.write("decision/one.md", "# One")

    _maybe_redescribe("decision")
    _maybe_redescribe("decision")
    _maybe_redescribe("decision")

    assert describe_spy["extracted"] == ["decision"], (
        "three calls within the debounce window must extract/write only once"
    )


def test_redescribe_fires_again_after_window_elapses(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    tmp_vault.write("decision/one.md", "# One")

    _maybe_redescribe("decision")
    assert describe_spy["extracted"] == ["decision"]

    import archiver_rag.watcher as w

    # Simulate the debounce window having elapsed without a real sleep.
    w._last_redescribed["decision"] -= (w._REDESCRIBE_DEBOUNCE_SECONDS + 1)
    _maybe_redescribe("decision")
    assert describe_spy["extracted"] == ["decision", "decision"]


def test_redescribe_debounce_is_independent_per_folder(tmp_vault, describe_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_describe_config", lambda: (True, 4, 6, 0.5, 1.0))
    tmp_vault.write("decision/one.md", "# One")
    tmp_vault.write("reference/one.md", "# One")

    _maybe_redescribe("decision")
    _maybe_redescribe("reference")

    assert describe_spy["extracted"] == ["decision", "reference"], (
        "debouncing one folder must not suppress a different folder"
    )


# ──────────────────────────────────────────────────────────────────────────────
# _maybe_cluster — now returns the folder it moved a note into (or None)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def cluster_spy(monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_cluster_config", lambda: (True, 5, 0.55, True))
    monkeypatch.setattr("archiver_rag.watcher._log", lambda m: None)
    monkeypatch.setattr(
        "archiver_rag.vault.reorganize.move_notes",
        lambda moves: {"moved": len(moves), "failed": 0, "succeeded": moves, "errors": []},
    )


def _suggest(monkeypatch, folder, reason="semantic", similarity=0.72):
    def _fake_suggest_folder(vault, note_path, *, threshold=0.55, type_fallback=True):
        return {
            "suggested_folder": folder,
            "similarity": similarity if folder else 0.0,
            "reason": reason if folder else "none",
            "scores": {folder: similarity} if folder else {},
        }
    monkeypatch.setattr("archiver_rag.graph.placement.suggest_folder", _fake_suggest_folder)


def test_maybe_cluster_returns_target_on_real_move(tmp_vault, cluster_spy, monkeypatch):
    _suggest(monkeypatch, "decision")
    note = tmp_vault.write("misc-notes.md", "# Loose")
    result = VaultHandler()._maybe_cluster(str(note))
    assert result == "decision"


def test_maybe_cluster_returns_none_when_disabled(tmp_vault, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._get_cluster_config", lambda: (False, 5, 0.55, True))
    note = tmp_vault.write("misc-notes.md", "# Loose")
    result = VaultHandler()._maybe_cluster(str(note))
    assert result is None


def test_maybe_cluster_returns_none_when_no_suggestion(tmp_vault, cluster_spy, monkeypatch):
    _suggest(monkeypatch, None)
    note = tmp_vault.write("lonely.md", "# Lonely")
    result = VaultHandler()._maybe_cluster(str(note))
    assert result is None


def test_maybe_cluster_returns_none_when_already_in_target(tmp_vault, cluster_spy, monkeypatch):
    _suggest(monkeypatch, "decision")
    note = tmp_vault.write("decision/already-there.md", "# Here")
    result = VaultHandler()._maybe_cluster(str(note))
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Handler wiring — structural events call _maybe_redescribe, on_modified never does
# ──────────────────────────────────────────────────────────────────────────────

class _FakeEvent:
    def __init__(self, src_path, is_directory: bool = False):
        self.src_path = str(src_path)
        self.is_directory = is_directory


class _FakeMoveEvent:
    def __init__(self, src, dst, is_directory: bool = False):
        self.src_path = str(src)
        self.dest_path = str(dst)
        self.is_directory = is_directory


@pytest.fixture
def handler_spy(monkeypatch):
    calls = {"redescribed": []}
    monkeypatch.setattr("archiver_rag.watcher.ingest_file", lambda p: None)
    monkeypatch.setattr("archiver_rag.watcher.auto_link", lambda p: None)
    monkeypatch.setattr("archiver_rag.watcher.VaultHandler._maybe_cluster", lambda self, p: None)
    monkeypatch.setattr("archiver_rag.watcher._is_indexed", lambda source: True)
    monkeypatch.setattr(
        "archiver_rag.watcher._maybe_redescribe",
        lambda rel_folder: calls["redescribed"].append(rel_folder),
    )

    class _FakeCollection:
        def delete(self, where=None):
            pass

    monkeypatch.setattr("archiver_rag.watcher.collection", _FakeCollection())
    monkeypatch.setattr(
        "archiver_rag.vault.notes.sweep_dead_links",
        lambda vault, stems: {"swept": [], "errors": []},
    )
    monkeypatch.setattr(
        "archiver_rag.vault.reorganize._update_wikilinks", lambda vault, old, new: None
    )
    return calls


def test_on_created_redescribes_containing_folder(tmp_vault, handler_spy):
    note = tmp_vault.write("decision/new.md", "# New")
    VaultHandler().on_created(_FakeEvent(note))
    assert handler_spy["redescribed"] == ["decision"]


def test_on_modified_never_redescribes(tmp_vault, handler_spy):
    """Body-only edits are not a membership change — locks in the confirmed scope."""
    note = tmp_vault.write("decision/existing.md", "# Existing")
    VaultHandler().on_modified(_FakeEvent(note))
    assert handler_spy["redescribed"] == []


def test_on_deleted_redescribes_source_folder(tmp_vault, handler_spy, monkeypatch):
    monkeypatch.setattr("archiver_rag.watcher._is_spurious_delete", lambda path, settle=1.0: False)
    note = tmp_vault.write("decision/doomed.md", "# Doomed")
    note.unlink()
    VaultHandler().on_deleted(_FakeEvent(note))
    assert handler_spy["redescribed"] == ["decision"]


def test_on_moved_redescribes_both_folders_on_cross_folder_move(tmp_vault, handler_spy):
    old = tmp_vault.root / "a" / "note.md"
    new = tmp_vault.write("b/note.md", "# Note")
    VaultHandler().on_moved(_FakeMoveEvent(old, new))
    assert set(handler_spy["redescribed"]) == {"a", "b"}


def test_on_moved_same_folder_atomic_save_redescribes_once(tmp_vault, handler_spy):
    """Atomic save within the same folder is not a membership change on either side,
    but the current wiring redescribes the (single, shared) destination folder once —
    it must not double-count the same folder as both src and dst."""
    note = tmp_vault.write("decision/note.md", "# Note")
    tmp = note.parent / "note.md.tmp.4242"
    VaultHandler().on_moved(_FakeMoveEvent(tmp, note))
    assert handler_spy["redescribed"] == ["decision"]


def test_on_moved_to_trash_redescribes_source_folder(tmp_vault, handler_spy):
    old = tmp_vault.root / "decision" / "doomed.md"
    trashed = tmp_vault.root / ".trash" / "doomed.md"
    VaultHandler().on_moved(_FakeMoveEvent(old, trashed))
    assert handler_spy["redescribed"] == ["decision"]
