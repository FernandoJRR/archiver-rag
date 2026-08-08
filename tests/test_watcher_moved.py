"""
Tests for watcher.on_moved.

The load-bearing case is the atomic save. Editors write `<name>.md.tmp.<rand>` and
rename it over the target, so the only event naming the real file is a move whose
SOURCE is not a .md. The old `not src.endswith(".md")` guard dropped those outright
and the note never reached the index — verified against the live vault with a
watchdog spy before this was written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archiver_rag.watcher import VaultHandler


class _FakeMoveEvent:
    def __init__(self, src, dst, is_directory=False):
        self.src_path = str(src)
        self.dest_path = str(dst)
        self.is_directory = is_directory


@pytest.fixture
def spy(monkeypatch):
    calls = {
        "ingested": [],
        "linked": [],
        "deleted": [],
        "swept": [],
        "relinked": [],
        "clustered": [],
    }
    monkeypatch.setattr(
        "archiver_rag.watcher.ingest_file",
        lambda p: calls["ingested"].append(Path(p).name),
    )
    monkeypatch.setattr(
        "archiver_rag.watcher.auto_link", lambda p: calls["linked"].append(Path(p).name)
    )
    monkeypatch.setattr(
        "archiver_rag.watcher.VaultHandler._maybe_cluster",
        lambda self, p: calls["clustered"].append(Path(p).name),
    )
    # Default: the destination is already indexed, i.e. an ordinary save.
    monkeypatch.setattr("archiver_rag.watcher._is_indexed", lambda source: True)

    class _FakeCollection:
        def delete(self, where=None):
            calls["deleted"].append(where)

    monkeypatch.setattr("archiver_rag.watcher.collection", _FakeCollection())
    monkeypatch.setattr(
        "archiver_rag.vault.notes.sweep_dead_links",
        lambda vault, stems: (
            calls["swept"].append(list(stems)) or {"swept": [], "errors": []}
        ),
    )
    monkeypatch.setattr(
        "archiver_rag.vault.reorganize._update_wikilinks",
        lambda vault, old, new: calls["relinked"].append((old, new)),
    )
    return calls


# ── the regression: atomic save must be ingested ─────────────────────────────


def test_atomic_save_is_ingested(tmp_vault, spy):
    """tmp.NNN -> note.md is how every editor save arrives. It must reach the index."""
    note = tmp_vault.write("decision/note.md", "# Note")
    tmp = note.parent / "note.md.tmp.17249.14dfc4f46218"
    VaultHandler().on_moved(_FakeMoveEvent(tmp, note))
    assert spy["ingested"] == ["note.md"], (
        "atomic save was dropped — note never indexed"
    )
    assert spy["linked"] == ["note.md"]
    # The temp file was never a note, so nothing to evict and no rename to propagate
    assert spy["deleted"] == []
    assert spy["relinked"] == []


def test_atomic_save_does_not_sweep(tmp_vault, spy):
    note = tmp_vault.write("decision/note.md", "# Note")
    tmp = note.parent / "note.md.tmp.999"
    VaultHandler().on_moved(_FakeMoveEvent(tmp, note))
    assert spy["swept"] == [], "a save must never sweep wikilinks"


# ── genuine rename still behaves as before ───────────────────────────────────


def test_rename_evicts_ingests_and_rewrites_links(tmp_vault, spy):
    old = tmp_vault.root / "decision" / "old.md"
    new = tmp_vault.write("decision/new.md", "# New")
    VaultHandler().on_moved(_FakeMoveEvent(old, new))
    assert spy["deleted"] == [{"source": "decision/old.md"}]
    assert spy["ingested"] == ["new.md"]
    assert spy["relinked"] == [("old", "new")]


def test_same_stem_move_does_not_rewrite_links(tmp_vault, spy):
    """Moving between folders keeps the stem — wikilinks still resolve, leave them."""
    old = tmp_vault.root / "a" / "note.md"
    new = tmp_vault.write("b/note.md", "# Note")
    VaultHandler().on_moved(_FakeMoveEvent(old, new))
    assert spy["ingested"] == ["note.md"]
    assert spy["relinked"] == []


# ── leaving note-space is a deletion ─────────────────────────────────────────


def test_move_to_trash_sweeps(tmp_vault, spy):
    """Obsidian deletes by moving into .trash/ — that must clean up the graph."""
    old = tmp_vault.root / "decision" / "doomed.md"
    trashed = tmp_vault.root / ".trash" / "doomed.md"
    VaultHandler().on_moved(_FakeMoveEvent(old, trashed))
    assert spy["deleted"] == [{"source": "decision/doomed.md"}]
    assert spy["swept"] == [["doomed"]]
    assert spy["ingested"] == [], ".trash must never be indexed"


def test_rename_to_non_markdown_sweeps(tmp_vault, spy):
    old = tmp_vault.root / "decision" / "note.md"
    new = tmp_vault.root / "decision" / "note.txt"
    VaultHandler().on_moved(_FakeMoveEvent(old, new))
    assert spy["swept"] == [["note"]]
    assert spy["ingested"] == []


# ── events that must be ignored ──────────────────────────────────────────────


def test_directory_move_ignored(tmp_vault, spy):
    VaultHandler().on_moved(
        _FakeMoveEvent(tmp_vault.root / "a", tmp_vault.root / "b", is_directory=True)
    )
    assert all(v == [] for v in spy.values())


def test_unrelated_move_ignored(tmp_vault, spy):
    """Neither end is a note — e.g. a temp file renamed to another temp file."""
    VaultHandler().on_moved(
        _FakeMoveEvent(tmp_vault.root / "x.tmp", tmp_vault.root / "y.tmp")
    )
    assert all(v == [] for v in spy.values())


def test_move_within_trash_ignored(tmp_vault, spy):
    a = tmp_vault.root / ".trash" / "a.md"
    b = tmp_vault.root / ".trash" / "b.md"
    VaultHandler().on_moved(_FakeMoveEvent(a, b))
    assert all(v == [] for v in spy.values())


# ── clustering gate: only genuinely new notes may be moved ───────────────────


def test_new_note_is_clustered(tmp_vault, spy, monkeypatch):
    """A note that reaches the vault via an atomic write and is not yet indexed."""
    monkeypatch.setattr("archiver_rag.watcher._is_indexed", lambda source: False)
    note = tmp_vault.write("fresh.md", "# Fresh")
    tmp = note.parent / "fresh.md.tmp.4242"
    VaultHandler().on_moved(_FakeMoveEvent(tmp, note))
    assert spy["clustered"] == ["fresh.md"]


def test_ordinary_save_is_not_clustered(tmp_vault, spy):
    """The whole point of the gate: on_moved fires on every save, and a save
    must never relocate the file the user is editing."""
    note = tmp_vault.write("decision/existing.md", "# Existing")
    tmp = note.parent / "existing.md.tmp.4242"
    VaultHandler().on_moved(_FakeMoveEvent(tmp, note))
    assert spy["ingested"] == ["existing.md"]
    assert spy["clustered"] == [], (
        "an edit triggered clustering — files would move as you type"
    )


def test_rename_is_not_clustered(tmp_vault, spy, monkeypatch):
    """A rename leaves the destination unindexed, but it is not a new note."""
    monkeypatch.setattr("archiver_rag.watcher._is_indexed", lambda source: False)
    old = tmp_vault.root / "decision" / "old.md"
    new = tmp_vault.write("decision/new.md", "# New")
    VaultHandler().on_moved(_FakeMoveEvent(old, new))
    assert spy["ingested"] == ["new.md"]
    assert spy["clustered"] == []


def test_indexing_error_does_not_cluster(tmp_vault, spy, monkeypatch):
    """_is_indexed returns True on error — clustering moves files, so uncertainty
    must resolve to 'do nothing'."""
    from archiver_rag import watcher

    class _Boom:
        def get(self, **kw):
            raise RuntimeError("chroma down")

        def delete(self, where=None):
            pass

    monkeypatch.setattr(watcher, "collection", _Boom())
    assert watcher._is_indexed("decision/whatever.md") is True
