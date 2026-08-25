"""Streamable HTTP transport (archiver_rag/mcp/http.py).

Driven in-process through httpx's ASGITransport, so nothing binds a port and no server
subprocess is spawned. The point of these tests is that the HTTP surface exposes the
*same* server object as stdio — StreamableHTTPSessionManager wraps the existing
low-level `Server`, so a second, drifting definition of the tools should be impossible
by construction, and `tools/list` returning the real seven is what pins that.
"""

from __future__ import annotations

import json

import httpx
import pytest

from archiver_rag.mcp.http import build_app

pytestmark = pytest.mark.anyio

HEADERS = {
    "Content-Type": "application/json",
    # Streamable HTTP requires the client to accept both, even in JSON-response mode.
    "Accept": "application/json, text/event-stream",
}

INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _payload(response: httpx.Response) -> dict:
    """Body as a dict, whether it came back as JSON or a single SSE frame."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no data frame in SSE body: {response.text!r}")
    return response.json()


async def _rpc(client, body: dict) -> dict:
    response = await client.post("/mcp", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    return _payload(response)


async def test_initialize_handshake():
    app = build_app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            result = (await _rpc(client, INIT))["result"]

    assert result["serverInfo"]["name"] == "obsidian-rag"


async def test_tools_list_exposes_the_same_seven_tools():
    app = build_app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await _rpc(client, INIT)
            body = await _rpc(
                client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )

    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {
        "search_vault", "vault_status", "move_notes", "log_note",
        "cluster_vault", "cluster_note", "get_connections",
    }


async def test_min_score_is_declared_so_a_client_can_actually_set_it():
    """The dispatch always read arguments['min_score'] but never advertised it."""
    app = build_app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await _rpc(client, INIT)
            body = await _rpc(
                client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )

    search = next(t for t in body["result"]["tools"] if t["name"] == "search_vault")
    assert "min_score" in search["inputSchema"]["properties"]


async def test_tools_call_runs_a_real_tool_against_a_temp_vault(tmp_vault):
    tmp_vault.write("decision/a.md", "---\ntype: decision\n---\nbody [[b]]")
    tmp_vault.write("decision/b.md", "---\ntype: decision\n---\nbody")

    app = build_app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await _rpc(client, INIT)
            body = await _rpc(client, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "vault_status", "arguments": {}},
            })

    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["structure"]["total_notes"] == 2


async def test_unknown_tool_is_an_error_not_a_crash():
    app = build_app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await _rpc(client, INIT)
            body = await _rpc(client, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            })

    assert body["result"]["isError"] is True
    assert "Unknown tool" in body["result"]["content"][0]["text"]


# ── DNS-rebinding allowlist ──────────────────────────────────────────────────

def test_protection_is_off_when_no_allowed_hosts_are_given():
    """The SDK rejects every request if protection is on with an empty allowlist."""
    from archiver_rag.mcp.http import _security_settings

    assert _security_settings(None) is None
    assert _security_settings([]) is None


def test_protection_turns_on_with_an_allowlist():
    from archiver_rag.mcp.http import _security_settings

    settings = _security_settings(["vault.internal.example"])
    assert settings.enable_dns_rebinding_protection is True
    assert "vault.internal.example" in settings.allowed_hosts
    assert "https://vault.internal.example" in settings.allowed_origins


async def test_allowed_host_is_accepted_and_others_rejected(tmp_vault):
    app = build_app(allowed_hosts=["vault.internal.example"])
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            ok = await client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**HEADERS, "Host": "vault.internal.example"},
            )
            bad = await client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**HEADERS, "Host": "evil.example.com"},
            )

    assert ok.status_code == 200
    assert bad.status_code == 421


def test_serve_http_adds_the_bind_address_to_the_allowlist(monkeypatch):
    """Otherwise --allowed-host makes the server unreachable at its own bind address."""
    from archiver_rag.mcp import http as mcp_http

    captured = {}
    monkeypatch.setattr(
        mcp_http, "build_app", lambda **kw: captured.update(kw) or "app"
    )
    monkeypatch.setitem(
        __import__("sys").modules, "uvicorn",
        type("U", (), {"run": staticmethod(lambda *a, **k: None)}),
    )

    mcp_http.serve_http(host="127.0.0.1", port=8077, allowed_hosts=["vault.internal.example"])

    assert "vault.internal.example" in captured["allowed_hosts"]
    assert "127.0.0.1:8077" in captured["allowed_hosts"]
