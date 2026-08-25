"""index_stats() — the index-vs-disk comparison behind `archiver-rag health`.

`collection.count()` alone could not distinguish a healthy index from one silently
missing notes, which is exactly the state the atomic-save watcher bug produced (three
notes on disk, absent from ChromaDB, found only by hand). These tests pin each drift
category and, critically, that a dead ChromaDB is reported rather than raised — health
must still be able to describe the vault when the index is unreachable.
"""

from __future__ import annotations

import os

import pytest

from archiver_rag.core.index_stats import in_sync, index_stats


class _StubCollection:
    """Minimal stand-in for the lazy ChromaDB proxy."""

    def __init__(self, metadatas, raises: Exception | None = None):
        self._metadatas = metadatas
        self._raises = raises

    def count(self):
        if self._raises:
            raise self._raises
        return len(self._metadatas)

    def get(self, include=None):
        if self._raises:
            raise self._raises
        return {"metadatas": self._metadatas}


@pytest.fixture
def stub_collection(monkeypatch):
    def _install(metadatas, raises=None):
        import archiver_rag.core.db as _db

        monkeypatch.setattr(_db, "collection", _StubCollection(metadatas, raises))

    return _install


def _chunks(vault, rel, n=2, mtime=None):
    """n chunks for one note, all carrying identical metadata — as ingest writes them."""
    if mtime is None:
        mtime = int(os.path.getmtime(vault / rel))
    return [{"source": rel, "mtime": mtime} for _ in range(n)]


def test_clean_vault_is_in_sync(tmp_vault, stub_collection):
    a = tmp_vault.write("decision/a.md", "---\ntype: decision\n---\nbody")
    b = tmp_vault.write("gotcha/b.md", "---\ntype: gotcha\n---\nbody")
    stub_collection(_chunks(tmp_vault.root, "decision/a.md") + _chunks(tmp_vault.root, "gotcha/b.md"))

    stats = index_stats(tmp_vault.root)
    assert stats["notes_on_disk"] == 2
    assert stats["indexed_notes"] == 2
    assert stats["chunks"] == 4
    assert in_sync(stats)
    assert stats["newest_mtime"]


def test_note_on_disk_but_not_indexed(tmp_vault, stub_collection):
    tmp_vault.write("decision/a.md", "body")
    tmp_vault.write("decision/never-indexed.md", "body")
    stub_collection(_chunks(tmp_vault.root, "decision/a.md"))

    stats = index_stats(tmp_vault.root)
    assert stats["missing_from_index"] == ["decision/never-indexed.md"]
    assert stats["counts"]["missing_from_index"] == 1
    assert not in_sync(stats)


def test_indexed_note_gone_from_disk(tmp_vault, stub_collection):
    tmp_vault.write("decision/a.md", "body")
    stub_collection(
        _chunks(tmp_vault.root, "decision/a.md")
        + [{"source": "decision/deleted.md", "mtime": 1}]
    )

    stats = index_stats(tmp_vault.root)
    assert stats["orphaned_in_index"] == ["decision/deleted.md"]
    assert stats["counts"]["orphaned_in_index"] == 1


def test_note_modified_since_indexing_is_stale(tmp_vault, stub_collection):
    tmp_vault.write("decision/a.md", "body")
    stub_collection([{"source": "decision/a.md", "mtime": 1}])

    stats = index_stats(tmp_vault.root)
    assert stats["stale"] == ["decision/a.md"]
    assert stats["counts"]["stale"] == 1


def test_sub_second_mtime_remainder_is_not_stale(tmp_vault, stub_collection):
    """ingest stores int(mtime); comparing against the raw float would flag forever."""
    p = tmp_vault.write("decision/a.md", "body")
    os.utime(p, (1_700_000_000.75, 1_700_000_000.75))
    stub_collection([{"source": "decision/a.md", "mtime": 1_700_000_000}])

    assert index_stats(tmp_vault.root)["stale"] == []


def test_folder_notes_and_hidden_dirs_are_not_counted(tmp_vault, stub_collection):
    """is_indexable_note is the single gate — _folder.md and .trash/ excluded for free."""
    tmp_vault.write("decision/a.md", "body")
    tmp_vault.write("decision/_folder.md", "---\ndescription_terms: [x]\n---")
    tmp_vault.write(".trash/old.md", "body")
    stub_collection(_chunks(tmp_vault.root, "decision/a.md"))

    stats = index_stats(tmp_vault.root)
    assert stats["notes_on_disk"] == 1
    assert in_sync(stats)


def test_unreachable_chromadb_is_reported_not_raised(tmp_vault, stub_collection):
    tmp_vault.write("decision/a.md", "body")
    stub_collection([], raises=FileNotFoundError("archiver-rag is not configured"))

    stats = index_stats(tmp_vault.root)
    assert "FileNotFoundError" in stats["error"]
    assert stats["notes_on_disk"] == 1  # vault side still reported
    assert stats["chunks"] == 0
    assert not in_sync(stats)
