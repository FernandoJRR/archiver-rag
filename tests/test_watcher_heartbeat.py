"""The watcher's heartbeat writes (archiver_rag/runtime.py wiring in watcher.py).

Two properties matter, and the negative one matters more. Positive: a real vault event
leaves a timestamped record with the right counter bumped. Negative: the events the
handlers deliberately ignore — a spurious delete from an atomic save, a _folder.md
sidecar write — must not register as vault activity, or `status` would report a busy
watcher on a vault where nothing happened. That is the same guard boundary
_is_spurious_delete and is_indexable_note already enforce for ingestion; the heartbeat
calls sit inside it, not outside.

conftest's autouse _no_real_home_paths points paths.cache_dir() at tmp_path, so nothing
here touches the real heartbeat file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archiver_rag import runtime
from archiver_rag.watcher import VaultHandler


class _FakeEvent:
    def __init__(self, src_path, is_directory: bool = False):
        self.src_path = str(src_path)
        self.is_directory = is_directory


class _FakeMoveEvent:
    def __init__(self, src_path, dest_path, is_directory: bool = False):
        self.src_path = str(src_path)
        self.dest_path = str(dest_path)
        self.is_directory = is_directory


@pytest.fixture
def quiet_watcher(monkeypatch):
    """Stub out everything the handlers do besides record the heartbeat."""
    class _FakeCollection:
        def delete(self, where=None):
            pass

        def get(self, **kw):
            return {"ids": []}

    monkeypatch.setattr("archiver_rag.watcher.collection", _FakeCollection())
    monkeypatch.setattr("archiver_rag.watcher.ingest_file", lambda *a, **kw: None)
    monkeypatch.setattr("archiver_rag.watcher.auto_link", lambda *a, **kw: None)
    monkeypatch.setattr("archiver_rag.watcher._maybe_redescribe", lambda *a, **kw: None)
    monkeypatch.setattr(
        "archiver_rag.watcher._refresh_folder_centroid", lambda *a, **kw: None
    )
    monkeypatch.setattr(VaultHandler, "_maybe_cluster", lambda self, path: None)
    monkeypatch.setattr(
        "archiver_rag.vault.notes.sweep_dead_links",
        lambda vault, stems: {"swept": [], "errors": []},
    )


def test_created_note_bumps_ingested(tmp_vault, quiet_watcher):
    note = tmp_vault.write("decision/a.md", "body")
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_created(_FakeEvent(note))

    state = runtime.read_state()
    assert state["counts"]["ingested"] == 1
    assert state["last_event_kind"] == "created"
    assert state["last_event"]
    # Handlers pass absolute paths; the heartbeat stores them vault-relative, since an
    # absolute path wraps in a terminal and reads badly in `status`.
    assert state["last_event_path"] == "decision/a.md"


def test_modified_note_bumps_ingested(tmp_vault, quiet_watcher):
    note = tmp_vault.write("decision/a.md", "body")
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_modified(_FakeEvent(note))

    assert runtime.read_state()["counts"]["ingested"] == 1


def test_atomic_save_arriving_as_a_move_bumps_ingested(tmp_vault, quiet_watcher):
    """tmp → note.md is how editors save; it is the only event naming the real file."""
    note = tmp_vault.write("decision/a.md", "body")
    tmp = tmp_vault.root / "decision" / "a.md.tmp.123"
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_moved(_FakeMoveEvent(tmp, note))

    state = runtime.read_state()
    assert state["counts"]["ingested"] == 1
    assert state["last_event_path"] == "decision/a.md"


def test_real_delete_bumps_deleted(tmp_vault, quiet_watcher):
    note = tmp_vault.write("decision/a.md", "body")
    note.unlink()
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_deleted(_FakeEvent(note))

    state = runtime.read_state()
    assert state["counts"]["deleted"] == 1
    assert state["last_event_kind"] == "deleted"


def test_spurious_delete_records_nothing(tmp_vault, quiet_watcher):
    """The file is already back on disk — a save, not a delete. No activity happened."""
    note = tmp_vault.write("decision/a.md", "body")
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_deleted(_FakeEvent(note))

    state = runtime.read_state()
    assert state["counts"]["deleted"] == 0
    assert state["last_event"] is None


def test_folder_note_write_records_nothing(tmp_vault, quiet_watcher):
    """_folder.md is a metadata artifact, not vault activity."""
    sidecar = tmp_vault.write("decision/_folder.md", "---\ndescription_terms: [x]\n---")
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_created(_FakeEvent(sidecar))
    VaultHandler().on_modified(_FakeEvent(sidecar))

    state = runtime.read_state()
    assert state["last_event"] is None
    assert state["counts"]["ingested"] == 0


def test_non_note_file_records_nothing(tmp_vault, quiet_watcher):
    other = tmp_vault.write("decision/image.png", "not a note")
    runtime.record_start(str(tmp_vault.root))

    VaultHandler().on_created(_FakeEvent(other))

    assert runtime.read_state()["last_event"] is None


def test_auto_placement_bumps_placed(tmp_vault, quiet_watcher, monkeypatch):
    """placed is recorded only where _maybe_cluster logs a real move."""
    note = tmp_vault.write("decision/a.md", "body")
    runtime.record_start(str(tmp_vault.root))

    def _fake_cluster(self, path):
        runtime.record_event("placed", "gotcha/a.md", counter="placed")
        return "gotcha"

    monkeypatch.setattr(VaultHandler, "_maybe_cluster", _fake_cluster)

    VaultHandler().on_created(_FakeEvent(note))

    state = runtime.read_state()
    assert state["counts"]["placed"] == 1
    assert state["counts"]["ingested"] == 1


def test_handlers_survive_an_unwritable_heartbeat(tmp_vault, quiet_watcher, monkeypatch, tmp_path):
    """Observability must never be able to break ingestion."""
    blocked = tmp_path / "blocked"
    blocked.write_text("file, not a dir", encoding="utf-8")
    import archiver_rag.paths as _paths

    monkeypatch.setattr(_paths, "cache_dir", lambda: blocked / "sub")

    note = tmp_vault.write("decision/a.md", "body")
    VaultHandler().on_created(_FakeEvent(note))  # must not raise
