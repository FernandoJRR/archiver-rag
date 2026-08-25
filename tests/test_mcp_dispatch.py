"""Tool dispatch, the vault write lock, and the thread offload (mcp/server.py).

Under stdio the transport serialized every request, so neither the lock nor the offload
was reachable — both exist because the HTTP transport can have several calls in flight.
The lock is a correctness guard (concurrent move_notes calls would interleave wikilink
rewrites across the whole vault and lose edits), and the offload keeps a slow embedding
call from stalling the event loop and every other client with it.
"""

from __future__ import annotations

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
        "search_vault", "vault_status", "move_notes", "log_note",
        "cluster_vault", "cluster_note", "get_connections",
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
