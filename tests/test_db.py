"""_LazyCollection — cache invalidation for the ChromaDB collection singleton.

A long-lived process (the detached HTTP MCP daemon; potentially a long-lived stdio
`serve`) that opens chromadb.PersistentClient once and never reconnects silently
serves stale search results forever: confirmed live, the daemon never saw writes
made by the CLI or the watcher after its own first connection, while the CLI (a
fresh process every invocation) always saw current data. These tests pin the fix —
_LazyCollection detects that chroma.sqlite3 changed and reconnects transparently,
using a signature captured *after* connecting (opening a client perturbs the
file's mtime once, confirmed live against the real install, so capturing the
signature before connecting would make every access look permanently stale).
"""

from __future__ import annotations

import json
import os
import threading

import chromadb
import pytest

from archiver_rag import paths
import archiver_rag.core.db as _db
from archiver_rag.core.db import _LazyCollection


def _write_config(chroma_dir) -> None:
    config_path = paths.config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"chroma_path": str(chroma_dir)}))


def _touch_forward(sqlite_path) -> None:
    """Bump mtime forward by a millisecond. Real writes already move it, but
    this removes any dependence on filesystem mtime resolution in a fast test."""
    st = os.stat(sqlite_path)
    os.utime(sqlite_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _second_writer(chroma_dir) -> None:
    """Stand-in for the CLI/watcher: a wholly independent client and collection
    that never touches the _LazyCollection instance under test, writing one
    document and nudging chroma.sqlite3's mtime forward."""
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection(
        name="obsidian_vault", metadata={"hnsw:space": "cosine"}
    )
    collection.add(ids=["a"], documents=["hello"], metadatas=[{"source": "a.md"}])
    _touch_forward(chroma_dir / "chroma.sqlite3")


class TestColdStart:
    def test_unconfigured_raises(self):
        lazy = _LazyCollection()
        with pytest.raises(FileNotFoundError):
            lazy.count()

    def test_first_access_connects(self, tmp_path):
        _write_config(tmp_path / "chroma")
        lazy = _LazyCollection()
        assert lazy.count() == 0


class TestSteadyState:
    def test_unchanged_file_does_not_reconnect(self, tmp_path, monkeypatch):
        _write_config(tmp_path / "chroma")
        lazy = _LazyCollection()
        lazy.count()  # first connect, unspied

        calls = []
        real_client = chromadb.PersistentClient
        monkeypatch.setattr(
            chromadb,
            "PersistentClient",
            lambda *a, **kw: (calls.append(1), real_client(*a, **kw))[1],
        )

        lazy.count()
        lazy.count()
        assert calls == []


class TestReconnectOnChange:
    def test_second_writer_is_picked_up_without_restart(self, tmp_path):
        """The actual bug, reproduced and fixed: a second, independent client
        writes a document; the original long-lived proxy must see it on its
        very next call, with no restart."""
        chroma_dir = tmp_path / "chroma"
        _write_config(chroma_dir)

        lazy = _LazyCollection()
        assert lazy.count() == 0

        _second_writer(chroma_dir)

        assert lazy.count() == 1

    def test_reconnect_logs(self, tmp_path, monkeypatch):
        chroma_dir = tmp_path / "chroma"
        _write_config(chroma_dir)
        lazy = _LazyCollection()
        lazy.count()

        logged = []
        monkeypatch.setattr(_db.utils, "log", lambda msg: logged.append(msg))

        _second_writer(chroma_dir)
        lazy.count()

        assert any("reconnected" in m for m in logged)


class TestFailSoft:
    def test_missing_sqlite_file_keeps_serving_existing_collection(self, tmp_path):
        chroma_dir = tmp_path / "chroma"
        _write_config(chroma_dir)
        lazy = _LazyCollection()
        assert lazy.count() == 0

        os.remove(chroma_dir / "chroma.sqlite3")

        # _sqlite_sig() can't stat the file -> None -> keep serving, don't raise.
        assert lazy.count() == 0

    def test_config_read_failure_keeps_serving_existing_collection(
        self, tmp_path, monkeypatch
    ):
        _write_config(tmp_path / "chroma")
        lazy = _LazyCollection()
        assert lazy.count() == 0

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(_db, "_get_chroma_path", _boom)

        assert lazy.count() == 0


class TestConcurrency:
    def test_bounded_reconnects_under_concurrent_access(self, tmp_path, monkeypatch):
        chroma_dir = tmp_path / "chroma"
        _write_config(chroma_dir)
        lazy = _LazyCollection()
        lazy.count()  # first connect, unspied

        _second_writer(chroma_dir)  # unspied — also uses PersistentClient

        calls = []
        calls_lock = threading.Lock()
        real_client = chromadb.PersistentClient

        def _spy(*a, **kw):
            with calls_lock:
                calls.append(1)
            return real_client(*a, **kw)

        monkeypatch.setattr(chromadb, "PersistentClient", _spy)

        results = []
        results_lock = threading.Lock()

        def _worker():
            r = lazy.count()
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == 1 for r in results)
        # Double-checked locking bounds this well under one reconnect per thread.
        assert len(calls) < 8
