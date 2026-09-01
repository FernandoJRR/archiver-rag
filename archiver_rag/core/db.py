import json
import os
import threading
from typing import Any

import chromadb

from archiver_rag import paths, utils


def _get_chroma_path() -> str:
    config_path = paths.config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            "archiver-rag is not configured. Run 'archiver-rag init' first."
        )
    return json.loads(config_path.read_text())["chroma_path"]


def _sqlite_sig(chroma_path: str) -> tuple[int, int] | None:
    """(mtime_ns, size) of chroma.sqlite3, or None if it can't be stat'd.

    Cheap enough to check on every access without instantiating a client.
    None (not an exception) signals "can't tell" to the caller, matching the
    fail-soft contract: a missing/unreadable file must never be read as "changed".
    """
    try:
        st = os.stat(os.path.join(chroma_path, "chroma.sqlite3"))
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class _LazyCollection:
    """Proxy that defers ChromaDB initialization to first use, and transparently
    reconnects when the on-disk data changes under a long-lived process.

    Keeps the MCP server importable even before 'archiver-rag init' has run,
    so tool errors reach the agent instead of crashing the server at startup.

    Without the freshness check, a process that opens this once and lives longer
    than one write cycle — the detached HTTP daemon, or a long-lived stdio `serve`
    — silently serves stale search results forever: opening chromadb.PersistentClient
    loads its collection state at connect time and never re-observes writes made by
    other processes afterward. The CLI never hit this because it's a fresh process
    per invocation. Staleness is detected via chroma.sqlite3's (mtime, size) — every
    add/delete hits that file immediately (its embeddings_queue table records every
    write) — not the HNSW segment files, which only get rewritten on infrequent
    compaction and can lag real writes by days.

    The signature is captured *after* (re)connecting, never before: opening a
    PersistentClient perturbs chroma.sqlite3's mtime once, even for a read-only
    session, so capturing pre-connect would make every access look permanently
    stale. Every freshness check fails soft — same contract as graph/centroids.py's
    fingerprint cache: a missed reconnect costs a few milliseconds on the next
    check, but a freshness check must never turn an already-working call into
    a failure.
    """

    def __init__(self) -> None:
        self._collection: chromadb.Collection | None = None
        self._sig: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def _connect(self, chroma_path: str) -> chromadb.Collection:
        client = chromadb.PersistentClient(path=chroma_path)
        collection = client.get_or_create_collection(
            name="obsidian_vault",
            metadata={"hnsw:space": "cosine"},
        )
        self._collection = collection
        self._sig = _sqlite_sig(chroma_path)
        return collection

    def _get(self) -> chromadb.Collection:
        if self._collection is None:
            # Cold start: let a missing/unreadable config raise, same as always.
            return self._connect(_get_chroma_path())

        try:
            chroma_path = _get_chroma_path()
            current = _sqlite_sig(chroma_path)
        except Exception:
            return self._collection  # can't check freshness; keep serving

        if current is None or current == self._sig:
            return self._collection

        with self._lock:
            # Re-check: another thread may have already reconnected while we
            # were waiting for the lock.
            current = _sqlite_sig(chroma_path)
            if current is None or current == self._sig:
                return self._collection
            collection = self._connect(chroma_path)
            utils.log("[db] reconnected to chroma_db (data changed since last connection)")
        return collection

    def _refresh_sig(self) -> None:
        """Re-capture the signature after a proxied call returns.

        Our own writes (.add/.delete/...) move chroma.sqlite3's mtime/size just
        like an external writer would. Without this, a write-heavy caller (index,
        sync, prune) would see its own previous call as "someone else changed the
        data" on every subsequent access and reconnect needlessly in a loop.
        Capturing the post-call state makes only genuinely external changes —
        ones that happen *between* our calls — show up as stale.
        """
        try:
            chroma_path = _get_chroma_path()
        except Exception:
            return
        self._sig = _sqlite_sig(chroma_path)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._get(), name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            result = attr(*args, **kwargs)
            self._refresh_sig()
            return result

        return _wrapped


collection = _LazyCollection()
