"""Watcher heartbeat — the only external evidence that the service is doing work.

`watch()`'s loop is a bare `observer.is_alive()` poll, so before this module the only
liveness signal outside the process was launchctl/systemd process state plus the mtime
of /tmp/archiver-rag.log. That answers "is a process loaded", never "did it index
anything since Tuesday". The watcher now writes a small JSON file after every event it
actually acts on, and `archiver-rag status` reads it.

Cache, not Data: the file is regenerated from scratch on the next `record_start`, so
losing it costs nothing but the counters since the last restart.

Every write fails soft, matching graph/centroids.py's contract — observability must
never be able to break ingestion. A status command that says "no heartbeat" is a far
better outcome than a watcher that dies because ~/.cache is read-only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

COUNTERS = ("ingested", "placed", "described", "swept", "deleted")


def state_path() -> Path:
    """Single indirection so tests redirect the heartbeat away from the real install."""
    from archiver_rag import paths

    return paths.cache_dir() / "runtime.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _relative(path: str, vault_path) -> str:
    """Store paths vault-relative — handlers pass absolutes, which wrap in a terminal.

    Done here rather than in each handler because the vault path is already in the state
    we just read, so it costs nothing on the watcher's hot path.
    """
    if not path or not vault_path:
        return path
    try:
        return str(Path(path).relative_to(vault_path))
    except ValueError:
        return path


def read_state() -> dict:
    """The heartbeat as last written, or {} if absent/unreadable/malformed."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(state: dict) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def record_start(vault_path: str) -> None:
    """Reset the heartbeat for a freshly started watcher process.

    Counters are per-process by design: `status` reports them as "since start", and a
    restart (including a KeepAlive restart after a crash) genuinely starts a new count.
    The stored pid is what lets status notice that launchd restarted us behind our back.
    """
    _write(
        {
            "pid": os.getpid(),
            "started_at": _now(),
            "vault_path": vault_path,
            "last_event": None,
            "last_event_kind": None,
            "last_event_path": None,
            "counts": {name: 0 for name in COUNTERS},
        }
    )


def record_event(kind: str, path: str = "", counter: str | None = None) -> None:
    """Stamp the last-event fields, and bump `counter` if one is named.

    Read-modify-write on every call rather than buffering in memory: the file is under
    a kilobyte and each event already costs an embed plus a Chroma write, so the
    marginal cost is noise — and buffering would lose everything the watcher did if it
    were killed, which is exactly when you go looking at this file.

    Called only after the handler's own guards pass, so a spurious delete or a
    _folder.md sidecar write never registers as vault activity.
    """
    state = read_state()
    if not state:
        # Watcher started before this module existed, or the file was wiped mid-run.
        # Record what we can rather than dropping the event entirely.
        state = {"pid": os.getpid(), "started_at": None, "counts": {}}
    state["last_event"] = _now()
    state["last_event_kind"] = kind
    state["last_event_path"] = _relative(path, state.get("vault_path"))
    if counter:
        counts = state.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        counts[counter] = int(counts.get(counter, 0)) + 1
        state["counts"] = counts
    _write(state)
