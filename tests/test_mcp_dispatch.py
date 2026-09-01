"""Tool dispatch, the vault write lock, and the thread offload (mcp/server.py).

Under stdio the transport serialized every request, so neither the lock nor the offload
was reachable — both exist because the HTTP transport can have several calls in flight.
The lock is a correctness guard (concurrent move_notes calls would interleave wikilink
rewrites across the whole vault and lose edits), and the offload keeps a slow embedding
call from stalling the event loop and every other client with it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import anyio
import pytest

from archiver_rag.mcp import server as mcp_server

pytestmark = pytest.mark.anyio


def test_read_only_tool_does_not_take_the_write_lock(tmp_vault, monkeypatch):
    """Reads must stay parallel — only mutation needs serializing."""
    tmp_vault.write("decision/a.md", "---\ntype: decision\n---\nbody")
    held = []

    class _SpyLock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mcp_server, "_VAULT_WRITE_LOCK", _SpyLock())

    mcp_server._dispatch("vault_status", {})
    assert held == []


def test_mutating_tool_takes_the_write_lock(tmp_vault, monkeypatch):
    held = []

    class _SpyLock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mcp_server, "_VAULT_WRITE_LOCK", _SpyLock())
    monkeypatch.setattr(
        "archiver_rag.vault.notes.log_note",
        lambda **kw: {"created": "decision/x.md"},
    )

    mcp_server._dispatch("log_note", {"title": "x", "content": "y"})
    assert held == [True]


def test_every_declared_tool_has_a_dispatch_branch(tmp_vault):
    """A tool advertised in list_tools but missing from _call would 'Unknown tool'."""
    declared = {
        "search_vault",
        "vault_status",
        "move_notes",
        "log_note",
        "cluster_vault",
        "cluster_note",
        "get_connections",
    }
    assert declared >= mcp_server._MUTATING_TOOLS

    for name in declared:
        try:
            mcp_server._dispatch(name, {})
        except ValueError as e:
            assert "Unknown tool" not in str(e), f"{name} has no dispatch branch"
        except Exception:
            pass  # Missing required args / empty vault are fine — routing is the point.


def test_unknown_tool_raises():
    with pytest.raises(ValueError, match="Unknown tool"):
        mcp_server._dispatch("does-not-exist", {})


def test_concurrent_mutations_are_serialized(tmp_vault, monkeypatch):
    """Two log_note calls must not overlap, or their vault writes could interleave."""
    overlap = []
    active = []

    def _slow_log_note(**kw):
        active.append(1)
        overlap.append(len(active))
        time.sleep(0.05)
        active.pop()
        return {"created": "decision/x.md"}

    monkeypatch.setattr("archiver_rag.vault.notes.log_note", _slow_log_note)

    def _run():
        mcp_server._dispatch("log_note", {"title": "t", "content": "c"})

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(overlap) == 1, f"mutations overlapped: {overlap}"


async def test_call_tool_does_not_block_the_event_loop(tmp_vault, monkeypatch):
    """A slow tool must not delay unrelated coroutines — that is the whole offload."""

    def _slow_status():
        time.sleep(0.4)
        return {"structure": {"total_notes": 0}}

    monkeypatch.setattr("archiver_rag.vault.health.vault_status", _slow_status)
    monkeypatch.setattr(mcp_server, "vault_status", _slow_status)

    ticks = 0

    async def _ticker():
        nonlocal ticks
        for _ in range(8):
            await anyio.sleep(0.02)
            ticks += 1

    async with anyio.create_task_group() as tg:
        tg.start_soon(_ticker)
        await mcp_server.call_tool("vault_status", {})

    # With a blocking handler the loop would be frozen and the ticker would not advance.
    assert ticks >= 5, f"event loop was blocked (only {ticks} ticks)"


async def test_call_tool_returns_text_content(tmp_vault):
    tmp_vault.write("decision/a.md", "---\ntype: decision\n---\nbody")

    result = await mcp_server.call_tool("vault_status", {})

    assert json.loads(result[0].text)["structure"]["total_notes"] == 1


# ── Server→client status notifications ──────────────────────────────────────


class _Recorder:
    """Records log/progress emissions in order. Stands in for Notifier in the
    per-tool emission tests — same interface, no loop/thread plumbing."""

    active = True

    def __init__(self):
        self.events = []

    def log(self, data):
        self.events.append(("log", data))

    def progress(self, progress, total, message=None):
        self.events.append(("progress", progress, total, message))


class _FakeSession:
    def __init__(self, fail=False):
        self.log_calls = []
        self.progress_calls = []
        self.fail = fail

    async def send_log_message(self, **kw):
        if self.fail:
            raise RuntimeError("send failed")
        self.log_calls.append(kw)

    async def send_progress_notification(self, **kw):
        if self.fail:
            raise RuntimeError("send failed")
        self.progress_calls.append(kw)


async def _flush_notifications():
    """Notifier sends are fire-and-forget (run_coroutine_threadsafe + done callback);
    give the loop a few ticks to run them."""
    for _ in range(10):
        await asyncio.sleep(0)


def _logs(rec):
    return [e[1] for e in rec.events if e[0] == "log"]


def _progress(rec):
    return [e for e in rec.events if e[0] == "progress"]


def test_search_vault_emits_exactly_two_log_messages(tmp_vault, monkeypatch):
    monkeypatch.setattr(mcp_server, "search_vault", lambda **kw: [{"r": 1}, {"r": 2}])
    rec = _Recorder()

    mcp_server._dispatch("search_vault", {"query": "q"}, notify=rec)

    assert _logs(rec) == ["searching: 'q'", "2 results, re-ranked"]
    assert _progress(rec) == []


def test_read_only_status_and_connections_emit_nothing(tmp_vault, monkeypatch):
    def _fake_status():
        return {}

    monkeypatch.setattr(mcp_server, "vault_status", _fake_status)
    monkeypatch.setattr(mcp_server, "get_connections", lambda *a, **kw: {})
    rec = _Recorder()

    mcp_server._dispatch("vault_status", {}, notify=rec)
    mcp_server._dispatch("get_connections", {"note": "a"}, notify=rec)

    assert rec.events == []


def test_log_note_emits_created_path(tmp_vault, monkeypatch):
    monkeypatch.setattr(
        "archiver_rag.vault.notes.log_note",
        lambda **kw: {"created": "decision/x.md"},
    )
    rec = _Recorder()

    mcp_server._dispatch("log_note", {"title": "x", "content": "y"}, notify=rec)

    assert _logs(rec) == ["created decision/x.md"]
    assert _progress(rec) == []


def test_move_notes_emits_per_move_progress_and_final_log(tmp_vault, monkeypatch):
    calls = []

    def _fake_move(moves):
        calls.append([m["source"] for m in moves])
        return {
            "moved": 1,
            "failed": 0,
            "succeeded": [
                {
                    "source": moves[0]["source"],
                    "destination": moves[0]["destination"],
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(mcp_server, "move_notes", _fake_move)
    rec = _Recorder()
    moves = [
        {"source": "a.md", "destination": "x/a.md"},
        {"source": "b.md", "destination": "x/b.md"},
    ]

    result = json.loads(
        mcp_server._dispatch("move_notes", {"moves": moves}, notify=rec)[0].text
    )

    # Live progress means one move_notes call per move, in order.
    assert calls == [["a.md"], ["b.md"]]
    assert rec.events == [
        ("log", "moving 2 files"),
        ("progress", 1, 2, "a.md → x/a.md"),
        ("progress", 2, 2, "b.md → x/b.md"),
        ("log", "moved 2/2"),
    ]
    # Merged result keeps the same shape a single batched call produces.
    assert result["moved"] == 2 and result["failed"] == 0


def test_move_notes_final_log_counts_failures(tmp_vault, monkeypatch):
    def _fake_move(moves):
        m = moves[0]
        if m["source"] == "b.md":
            return {
                "moved": 0,
                "failed": 1,
                "succeeded": [],
                "errors": [{"source": "b.md", "error": "nope"}],
            }
        return {"moved": 1, "failed": 0, "succeeded": [m], "errors": []}

    monkeypatch.setattr(mcp_server, "move_notes", _fake_move)
    rec = _Recorder()

    mcp_server._dispatch(
        "move_notes",
        {
            "moves": [
                {"source": "a.md", "destination": "x/a.md"},
                {"source": "b.md", "destination": "x/b.md"},
            ]
        },
        notify=rec,
    )

    assert _logs(rec)[-1] == "moved 1/2"


def test_move_notes_null_notifier_keeps_single_batched_call(tmp_vault, monkeypatch):
    """Without an active notifier the pre-notification batched call is preserved."""
    calls = []

    def _fake_move(moves):
        calls.append(list(moves))
        return {
            "moved": len(moves),
            "failed": 0,
            "succeeded": moves,
            "errors": [],
        }

    monkeypatch.setattr(mcp_server, "move_notes", _fake_move)
    moves = [
        {"source": "a.md", "destination": "x/a.md"},
        {"source": "b.md", "destination": "x/b.md"},
    ]

    result = json.loads(mcp_server._dispatch("move_notes", {"moves": moves})[0].text)

    assert calls == [moves]  # one batched call, not per-move
    assert result == {"moved": 2, "failed": 0, "succeeded": moves, "errors": []}


def test_cluster_vault_apply_emits_analysis_and_per_move_progress(
    tmp_vault, monkeypatch
):
    monkeypatch.setattr(
        "archiver_rag.graph.clustering.cluster_vault",
        lambda min_cluster_size=2: {
            "total_notes": 4,
            "total_clusters": 1,
            "unclustered": [],
            "clusters": [
                {
                    "name": "x",
                    "size": 2,
                    "notes": ["a.md", "b.md"],
                    "suggested_folder": "x",
                }
            ],
        },
    )
    moved = []

    def _fake_move(moves):
        moved.append(moves[0]["source"])
        return {"moved": 1, "failed": 0, "succeeded": moves, "errors": []}

    monkeypatch.setattr(mcp_server, "move_notes", _fake_move)
    rec = _Recorder()

    result = json.loads(
        mcp_server._dispatch("cluster_vault", {"apply": True}, notify=rec)[0].text
    )

    assert _logs(rec) == ["analyzing 4 notes"]
    assert _progress(rec) == [
        ("progress", 1, 2, "moved 1/2"),
        ("progress", 2, 2, "moved 2/2"),
    ]
    assert moved == ["a.md", "b.md"]
    assert len(result["moves"]) == 2


def test_cluster_vault_apply_null_notifier_uses_apply_clusters(tmp_vault, monkeypatch):
    """Without an active notifier the tested apply_clusters path is kept verbatim."""
    monkeypatch.setattr(
        "archiver_rag.graph.clustering.cluster_vault",
        lambda min_cluster_size=2: {
            "total_notes": 1,
            "total_clusters": 1,
            "unclustered": [],
            "clusters": [
                {
                    "name": "x",
                    "size": 1,
                    "notes": ["a.md"],
                    "suggested_folder": "x",
                }
            ],
        },
    )
    used = []
    monkeypatch.setattr(
        "archiver_rag.graph.clustering.apply_clusters",
        lambda clusters: used.append(clusters) or [],
    )

    result = json.loads(mcp_server._dispatch("cluster_vault", {"apply": True})[0].text)

    assert result["moves"] == []
    assert len(used) == 1  # apply_clusters was called, not the split loop


def test_cluster_note_emits_placing_and_move_reports(tmp_vault, monkeypatch):
    tmp_vault.write("scratch/a.md", "---\ntype: decision\n---\nbody")

    def _fake_cluster_note(note, apply=False):
        # Simulate the internal move physically landing (source gone, dest exists).
        (tmp_vault.root / "scratch" / "a.md").unlink()
        tmp_vault.write("target/a.md", "---\ntype: decision\n---\nbody")
        return {"note": note, "suggested_folder": "target", "similarity": 0.9}

    monkeypatch.setattr(
        "archiver_rag.graph.clustering.cluster_note", _fake_cluster_note
    )
    rec = _Recorder()

    mcp_server._dispatch("cluster_note", {"note": "a.md", "apply": True}, notify=rec)

    assert rec.events == [
        ("log", "placing 'a.md'"),
        ("progress", 1, 1, "moved to target"),
        ("log", "moved a.md → target"),
    ]


def test_cluster_note_no_move_reports_nothing_after_placing(tmp_vault, monkeypatch):
    tmp_vault.write("target/a.md", "---\ntype: decision\n---\nbody")
    monkeypatch.setattr(
        "archiver_rag.graph.clustering.cluster_note",
        lambda note, apply=False: {
            "note": note,
            "suggested_folder": "target",
            "similarity": 0.9,
        },
    )
    rec = _Recorder()

    mcp_server._dispatch("cluster_note", {"note": "a.md", "apply": True}, notify=rec)

    # Note already sits in the suggested folder — cluster_note's move is a no-op,
    # so only the 'placing' log fires.
    assert _logs(rec) == ["placing 'a.md'"]
    assert _progress(rec) == []


def test_dispatch_without_notify_defaults_to_null_and_is_identical(
    tmp_vault, monkeypatch
):
    """Pre-notification behaviour: no notify kwarg → null notifier, same result."""
    monkeypatch.setattr(mcp_server, "search_vault", lambda **kw: [{"r": 1}])
    tmp_vault.write("decision/a.md", "---\ntype: decision\n---\nbody")

    a = mcp_server._dispatch("search_vault", {"query": "q"})[0].text
    b = mcp_server._dispatch(
        "search_vault", {"query": "q"}, notify=mcp_server._NULL_NOTIFIER
    )[0].text

    assert a == b


async def test_notifier_log_and_progress_reach_the_session():
    session = _FakeSession()
    loop = asyncio.get_running_loop()
    notifier = mcp_server.Notifier(
        session,
        loop,
        "search_vault",
        request_id=7,
        progress_token="tok",  # noqa: S106
    )

    notifier.log("hello")
    notifier.progress(1, 3, "step")
    await _flush_notifications()

    assert session.log_calls == [
        {
            "level": "info",
            "data": "hello",
            "logger": "search_vault",
            "related_request_id": "7",
        }
    ]
    assert session.progress_calls == [
        {
            "progress_token": "tok",
            "progress": 1,
            "total": 3,
            "message": "step",
            "related_request_id": "7",
        }
    ]


async def test_notifier_progress_without_token_is_dropped():
    session = _FakeSession()
    notifier = mcp_server.Notifier(
        session,
        asyncio.get_running_loop(),
        "move_notes",
        request_id="1",
        progress_token=None,
    )

    notifier.progress(1, 1, "x")
    notifier.log("still logged")
    await _flush_notifications()

    assert session.progress_calls == []
    assert len(session.log_calls) == 1  # logging does not need a token


async def test_notifier_failure_never_fails_the_tool_call(tmp_vault, monkeypatch):
    """A failing send is swallowed and reported via utils.log — the heartbeat's
    fail-soft contract. The tool result itself is untouched."""
    captured = []
    monkeypatch.setattr("archiver_rag.utils.log", lambda msg: captured.append(msg))
    monkeypatch.setattr(mcp_server, "search_vault", lambda **kw: [{"r": 1}])

    session = _FakeSession(fail=True)
    notifier = mcp_server.Notifier(
        session, asyncio.get_running_loop(), "search_vault", request_id="1"
    )

    result = mcp_server._dispatch("search_vault", {"query": "q"}, notify=notifier)
    await _flush_notifications()

    assert json.loads(result[0].text) == [{"r": 1}]
    assert any("notification failed for search_vault" in m for m in captured)


async def test_notifier_respects_client_requested_minimum_level(monkeypatch):
    session = _FakeSession()
    notifier = mcp_server.Notifier(
        session, asyncio.get_running_loop(), "search_vault", request_id="1"
    )

    monkeypatch.setattr(mcp_server, "_min_log_level", "notice")
    notifier.log("below threshold")
    await _flush_notifications()
    assert session.log_calls == []

    monkeypatch.setattr(mcp_server, "_min_log_level", "info")
    notifier.log("at threshold")
    await _flush_notifications()
    assert len(session.log_calls) == 1


def test_capabilities_advertise_logging():
    from mcp.server.lowlevel.server import NotificationOptions

    caps = mcp_server.app.get_capabilities(NotificationOptions(), {})

    assert caps.logging is not None  # advertised by the set_logging_level handler


async def test_set_logging_level_handler_stores_the_level(monkeypatch):
    from mcp import types

    monkeypatch.setattr(mcp_server, "_min_log_level", "info")
    handler = mcp_server.app.request_handlers[types.SetLevelRequest]
    req = types.SetLevelRequest(
        method="logging/setLevel",
        params=types.SetLevelRequestParams(level="error"),
    )

    await handler(req)

    assert mcp_server._min_log_level == "error"


def test_bridge_notifier_outside_request_context_is_null():
    assert mcp_server._bridge_notifier("vault_status") is mcp_server._NULL_NOTIFIER


async def test_call_tool_bridges_notifier_from_request_context(tmp_vault, monkeypatch):
    """Full bridge: request context → notifier → worker-thread emission → loop.

    Regression guard for resolving the context the wrong way: Server.request_context
    is a @property in this SDK version, so calling it as a method raises
    TypeError('RequestContext' object is not callable) — only visible inside a real
    request, never in the direct-call tests. The module-level request_ctx.get() is
    the correct access path."""
    from types import SimpleNamespace

    import mcp.server.lowlevel.server as lowlevel

    monkeypatch.setattr(mcp_server, "search_vault", lambda **kw: [{"r": 1}])

    session = _FakeSession()
    fake_ctx = SimpleNamespace(
        session=session,
        request_id=42,
        meta=SimpleNamespace(progressToken="tok"),
    )
    token = lowlevel.request_ctx.set(fake_ctx)  # type: ignore[arg-type]
    try:
        await mcp_server.call_tool("search_vault", {"query": "q"})
    finally:
        lowlevel.request_ctx.reset(token)
    await _flush_notifications()

    assert [c["data"] for c in session.log_calls] == [
        "searching: 'q'",
        "1 results, re-ranked",
    ]
    # No _call emissions call progress for search_vault — token unused here.
    assert session.progress_calls == []
