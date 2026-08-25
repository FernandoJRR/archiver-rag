"""Watcher heartbeat (archiver_rag/runtime.py).

The heartbeat is pure observability, so the load-bearing property is not that it is
accurate — it is that it can never take the watcher down. Every failure mode here
(missing file, corrupt file, unwritable directory) must resolve to a quiet {} or a
no-op, never an exception reaching an event handler.

conftest's autouse _no_real_home_paths already points paths.cache_dir() at tmp_path,
so these never touch the real ~/.cache/archiver-rag.
"""

from __future__ import annotations

import json
import os

from archiver_rag import runtime


def test_read_state_is_empty_when_never_started():
    assert runtime.read_state() == {}


def test_record_start_writes_pid_and_zeroed_counters():
    runtime.record_start("/tmp/some-vault")
    state = runtime.read_state()

    assert state["pid"] == os.getpid()
    assert state["vault_path"] == "/tmp/some-vault"
    assert state["started_at"]
    assert state["last_event"] is None
    assert state["counts"] == {name: 0 for name in runtime.COUNTERS}


def test_record_event_stamps_last_event_and_bumps_counter():
    runtime.record_start("/tmp/some-vault")
    runtime.record_event("created", "decision/foo.md", counter="ingested")
    runtime.record_event("modified", "decision/foo.md", counter="ingested")
    runtime.record_event("placed", "gotcha/foo.md", counter="placed")

    state = runtime.read_state()
    assert state["last_event_kind"] == "placed"
    assert state["last_event_path"] == "gotcha/foo.md"
    assert state["last_event"]
    assert state["counts"]["ingested"] == 2
    assert state["counts"]["placed"] == 1
    assert state["counts"]["deleted"] == 0


def test_record_event_without_counter_only_stamps():
    runtime.record_start("/tmp/some-vault")
    runtime.record_event("modified", "a.md")

    assert runtime.read_state()["counts"] == {n: 0 for n in runtime.COUNTERS}


def test_record_event_survives_a_missing_start():
    """A watcher already running when the file is wiped must still record something."""
    runtime.record_event("created", "a.md", counter="ingested")

    state = runtime.read_state()
    assert state["counts"]["ingested"] == 1
    assert state["started_at"] is None


def test_read_state_is_empty_on_corrupt_json():
    path = runtime.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert runtime.read_state() == {}


def test_read_state_is_empty_when_file_holds_a_non_dict():
    path = runtime.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert runtime.read_state() == {}


def test_writes_never_raise_when_the_cache_dir_is_unwritable(monkeypatch, tmp_path):
    """The whole point: observability must not be able to kill ingestion."""
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory", encoding="utf-8")

    import archiver_rag.paths as _paths

    monkeypatch.setattr(_paths, "cache_dir", lambda: blocked / "sub")

    runtime.record_start("/tmp/some-vault")  # must not raise
    runtime.record_event("created", "a.md", counter="ingested")  # must not raise
    assert runtime.read_state() == {}
