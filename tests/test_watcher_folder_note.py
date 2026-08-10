"""Tests for watcher's _folder.md handling.

When _folder.md is modified (directly or via atomic save):
  - centroid is refreshed
  - ingest_file and auto_link are NOT called

When _folder.md is deleted:
  - centroid is dropped (with spurious-delete guard)

These tests stub out embed() and the centroid cache to avoid real I/O.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archiver_rag.watcher import VaultHandler
from archiver_rag.utils import FOLDER_NOTE_NAME


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_event(src: str, is_directory: bool = False):
    e = MagicMock()
    e.src_path = src
    e.is_directory = is_directory
    return e


def _make_moved_event(src: str, dst: str, is_directory: bool = False):
    e = MagicMock()
    e.src_path = src
    e.dest_path = dst
    e.is_directory = is_directory
    return e


def _sidecar_path(vault: Path, rel_folder: str) -> str:
    return str(vault / rel_folder / FOLDER_NOTE_NAME)


# ──────────────────────────────────────────────────────────────────────────────
# on_modified — direct _folder.md write
# ──────────────────────────────────────────────────────────────────────────────

def test_on_modified_folder_note_refreshes_centroid(tmp_vault, monkeypatch):
    refreshed = []
    monkeypatch.setattr(
        "archiver_rag.watcher._refresh_folder_centroid",
        lambda path: refreshed.append(path),
    )
    ingest_spy = []
    monkeypatch.setattr("archiver_rag.watcher.ingest_file", lambda p: ingest_spy.append(p))
    link_spy = []
    monkeypatch.setattr("archiver_rag.watcher.auto_link", lambda p: link_spy.append(p))

    vault = Path(tmp_vault.root)
    path = _sidecar_path(vault, "gotcha")
    (vault / "gotcha").mkdir(exist_ok=True)
    Path(path).write_text("---\ndescription_terms: [watcher]\nsource: auto\n---\n")

    handler = VaultHandler()
    handler.on_modified(_make_event(path))

    assert refreshed, "centroid should have been refreshed"
    assert not ingest_spy, "ingest_file must NOT be called for _folder.md"
    assert not link_spy, "auto_link must NOT be called for _folder.md"


def test_on_created_folder_note_refreshes_centroid(tmp_vault, monkeypatch):
    refreshed = []
    monkeypatch.setattr(
        "archiver_rag.watcher._refresh_folder_centroid",
        lambda path: refreshed.append(path),
    )
    monkeypatch.setattr("archiver_rag.watcher.ingest_file", lambda p: None)
    monkeypatch.setattr("archiver_rag.watcher.auto_link", lambda p: None)

    vault = Path(tmp_vault.root)
    path = _sidecar_path(vault, "gotcha")
    (vault / "gotcha").mkdir(exist_ok=True)
    Path(path).write_text("---\ndescription_terms: [watcher]\nsource: auto\n---\n")

    handler = VaultHandler()
    handler.on_created(_make_event(path))

    assert refreshed


# ──────────────────────────────────────────────────────────────────────────────
# on_moved — atomic save of _folder.md (tmp → sidecar)
# ──────────────────────────────────────────────────────────────────────────────

def test_on_moved_atomic_save_of_folder_note_refreshes_dst(tmp_vault, monkeypatch):
    """Atomic save arrives as moved(tmp→sidecar). dst is _folder.md → refresh."""
    refreshed = []
    monkeypatch.setattr(
        "archiver_rag.watcher._refresh_folder_centroid",
        lambda path: refreshed.append(path),
    )
    dropped = []
    monkeypatch.setattr(
        "archiver_rag.graph.centroids.drop_centroid",
        lambda rel: dropped.append(rel) or True,
    )

    vault = Path(tmp_vault.root)
    src = str(vault / "gotcha" / "_folder.md.tmp.12345.abc")  # temp file (not a folder note)
    dst = _sidecar_path(vault, "gotcha")

    handler = VaultHandler()
    handler.on_moved(_make_moved_event(src, dst))

    assert any(dst in r for r in refreshed), "dst refresh not triggered on atomic save"
    assert not dropped, "centroid should not be dropped on atomic save"


def test_on_moved_folder_note_moved_away_drops_centroid(tmp_vault, monkeypatch):
    """_folder.md renamed away from its folder → drop centroid."""
    refreshed = []
    monkeypatch.setattr(
        "archiver_rag.watcher._refresh_folder_centroid",
        lambda path: refreshed.append(path),
    )
    dropped = []
    monkeypatch.setattr(
        "archiver_rag.graph.centroids.drop_centroid",
        lambda rel: dropped.append(rel) or True,
    )
    # also mock _log so we don't need the real one
    monkeypatch.setattr("archiver_rag.watcher._log", lambda m: None)

    vault = Path(tmp_vault.root)
    src = _sidecar_path(vault, "gotcha")   # was a folder note
    dst = str(vault / "gotcha" / "something-else.md")  # no longer a folder note

    handler = VaultHandler()
    handler.on_moved(_make_moved_event(src, dst))

    assert dropped, "centroid should be dropped when sidecar moves away"
    assert not refreshed, "refresh should not fire when sidecar is moved away"


# ──────────────────────────────────────────────────────────────────────────────
# on_deleted — _folder.md removed
# ──────────────────────────────────────────────────────────────────────────────

def test_on_deleted_folder_note_drops_centroid(tmp_vault, monkeypatch):
    """Genuine delete of _folder.md → drop centroid."""
    dropped = []
    monkeypatch.setattr(
        "archiver_rag.graph.centroids.drop_centroid",
        lambda rel: dropped.append(rel) or True,
    )
    monkeypatch.setattr("archiver_rag.watcher._log", lambda m: None)
    # Make the spurious-delete guard immediately return False (it's a real delete)
    monkeypatch.setattr(
        "archiver_rag.watcher._is_spurious_delete", lambda path, settle=1.0: False
    )

    vault = Path(tmp_vault.root)
    path = _sidecar_path(vault, "gotcha")

    handler = VaultHandler()
    handler.on_deleted(_make_event(path))

    assert dropped


def test_on_deleted_folder_note_spurious_guard_respected(tmp_vault, monkeypatch):
    """Spurious delete of _folder.md must not drop centroid."""
    dropped = []
    monkeypatch.setattr(
        "archiver_rag.graph.centroids.drop_centroid",
        lambda rel: dropped.append(rel) or True,
    )
    monkeypatch.setattr(
        "archiver_rag.watcher._is_spurious_delete", lambda path, settle=1.0: True
    )

    vault = Path(tmp_vault.root)
    path = _sidecar_path(vault, "gotcha")

    handler = VaultHandler()
    handler.on_deleted(_make_event(path))

    assert not dropped, "centroid must not be dropped on spurious delete"


# ──────────────────────────────────────────────────────────────────────────────
# Real notes still work normally alongside _folder.md branches
# ──────────────────────────────────────────────────────────────────────────────

def test_on_modified_real_note_still_ingested(tmp_vault, monkeypatch):
    """Adding the _folder.md branch must not break regular note handling."""
    refreshed = []
    monkeypatch.setattr(
        "archiver_rag.watcher._refresh_folder_centroid",
        lambda path: refreshed.append(path),
    )
    ingested = []
    monkeypatch.setattr("archiver_rag.watcher.ingest_file", lambda p: ingested.append(p))
    monkeypatch.setattr("archiver_rag.watcher.auto_link", lambda p: None)

    vault = Path(tmp_vault.root)
    note = tmp_vault.write("gotcha/real-note.md", "# Real Note\nContent.")

    handler = VaultHandler()
    handler.on_modified(_make_event(str(note)))

    assert ingested, "real note must still be ingested"
    assert not refreshed, "refresh must not fire for regular notes"
