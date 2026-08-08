"""
Tests for the spurious-delete guard in watcher.on_deleted.

Atomic saves (temp file + os.replace) make watchdog emit a `deleted` event *after*
the file is already back on disk. Acting on it evicted the note from ChromaDB on every
save and would sweep live wikilinks out of unrelated notes. _is_spurious_delete is the
guard; these tests pin both halves of it — a save must be a no-op, a real delete must
still clean up.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from archiver_rag.watcher import VaultHandler, _is_spurious_delete


class _FakeEvent:
    """Minimal stand-in for a watchdog FileSystemEvent."""

    def __init__(self, src_path: Path | str, is_directory: bool = False):
        self.src_path = str(src_path)
        self.is_directory = is_directory


@pytest.fixture
def spy(monkeypatch):
    """Capture collection.delete and sweep_dead_links instead of running them."""
    calls = {"deleted": [], "swept": []}

    class _FakeCollection:
        def delete(self, where=None):
            calls["deleted"].append(where)

    monkeypatch.setattr("archiver_rag.watcher.collection", _FakeCollection())
    monkeypatch.setattr(
        "archiver_rag.vault.notes.sweep_dead_links",
        lambda vault, stems: calls["swept"].append(list(stems)) or {"swept": [], "errors": []},
    )
    return calls


# ── _is_spurious_delete in isolation ─────────────────────────────────────────

def test_existing_file_is_spurious_immediately(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("here")
    start = time.monotonic()
    assert _is_spurious_delete(f, settle=5.0) is True
    # Must not wait out the settle window when the file is already back
    assert time.monotonic() - start < 0.5


def test_missing_file_is_a_real_delete(tmp_path):
    f = tmp_path / "gone.md"
    assert _is_spurious_delete(f, settle=0.15) is False


def test_file_reappearing_mid_window_is_spurious(tmp_path):
    f = tmp_path / "note.md"

    def _recreate():
        time.sleep(0.15)
        f.write_text("back")

    t = threading.Thread(target=_recreate)
    t.start()
    try:
        assert _is_spurious_delete(f, settle=3.0) is True
    finally:
        t.join()


# ── on_deleted end to end ────────────────────────────────────────────────────

def test_save_does_not_evict_or_sweep(tmp_vault, spy):
    """The regression: a spurious delete for a file still on disk must do nothing."""
    note = tmp_vault.write("decision/alive.md", "# Alive")
    VaultHandler().on_deleted(_FakeEvent(note))
    assert spy["deleted"] == [], "spurious delete evicted the note from the index"
    assert spy["swept"] == [], "spurious delete swept wikilinks"


def test_real_delete_evicts_and_sweeps(tmp_vault, spy):
    note = tmp_vault.write("decision/doomed.md", "# Doomed")
    note.unlink()
    VaultHandler().on_deleted(_FakeEvent(note))
    assert spy["deleted"] == [{"source": "decision/doomed.md"}]
    assert spy["swept"] == [["doomed"]]


def test_directory_event_ignored(tmp_vault, spy):
    VaultHandler().on_deleted(_FakeEvent(tmp_vault.root / "folder", is_directory=True))
    assert spy["deleted"] == []
    assert spy["swept"] == []


def test_non_markdown_ignored(tmp_vault, spy):
    VaultHandler().on_deleted(_FakeEvent(tmp_vault.root / "image.png"))
    assert spy["deleted"] == []
    assert spy["swept"] == []


def test_hidden_path_ignored(tmp_vault, spy):
    """A delete inside .trash/ must never sweep — that is where deleted notes land."""
    VaultHandler().on_deleted(_FakeEvent(tmp_vault.root / ".trash" / "trashed.md"))
    assert spy["deleted"] == []
    assert spy["swept"] == []
