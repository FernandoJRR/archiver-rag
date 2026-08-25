"""`archiver-rag serve` transport flags, and register_mcp's two entry shapes.

The load-bearing property is backwards compatibility: stdio stays the default, so every
existing install and the ~/.claude.json entry written by `archiver-rag init` keep working
untouched. The rest is about not binding somewhere surprising — a corrupt config must
fall back to loopback, and a non-loopback bind must say out loud that there is no auth.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from archiver_rag.cli import app, _is_loopback
from archiver_rag.mcp import http as mcp_http

runner = CliRunner()


@pytest.fixture
def spy_serve(monkeypatch):
    """Capture serve_http's kwargs instead of binding a socket."""
    calls = {}
    monkeypatch.setattr(mcp_http, "serve_http", lambda **kw: calls.update(kw))
    return calls


def test_stdio_is_the_default(monkeypatch):
    """Existing installs must not change behaviour when serve gains flags."""
    ran = []
    monkeypatch.setattr("archiver_rag.mcp.server.main", lambda: ran.append(True))
    monkeypatch.setattr("asyncio.run", lambda coro: ran.append("asyncio.run"))

    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert "asyncio.run" in ran


def test_http_uses_loopback_defaults(spy_serve):
    result = runner.invoke(app, ["serve", "--transport", "http"])

    assert result.exit_code == 0
    assert spy_serve["host"] == "127.0.0.1"
    assert spy_serve["port"] == 8077
    assert spy_serve["path"] == "/mcp"
    assert spy_serve["stateless"] is True


def test_flags_override_defaults(spy_serve):
    runner.invoke(app, [
        "serve", "--transport", "http", "--host", "0.0.0.0",
        "--port", "9999", "--path", "/rag", "--stateful",
        "--allowed-host", "vault.internal.example",
    ])

    assert spy_serve["host"] == "0.0.0.0"
    assert spy_serve["port"] == 9999
    assert spy_serve["path"] == "/rag"
    assert spy_serve["stateless"] is False
    assert spy_serve["allowed_hosts"] == ["vault.internal.example"]


def test_non_loopback_bind_warns_about_missing_auth(spy_serve):
    result = runner.invoke(app, ["serve", "--transport", "http", "--host", "0.0.0.0"])

    assert "NO authentication" in result.output
    assert "reverse proxy" in result.output


def test_loopback_bind_does_not_warn(spy_serve):
    result = runner.invoke(app, ["serve", "--transport", "http"])

    assert "NO authentication" not in result.output


def test_corrupt_config_falls_back_to_loopback(spy_serve, monkeypatch):
    """load_config() returns {} on error — it must not leave host unset or wide open."""
    monkeypatch.setattr("archiver_rag.utils.load_config", lambda: {})

    runner.invoke(app, ["serve", "--transport", "http"])
    assert spy_serve["host"] == "127.0.0.1"


def test_config_supplies_defaults(spy_serve, monkeypatch):
    monkeypatch.setattr(
        "archiver_rag.utils.load_config",
        lambda: {"http_host": "127.0.0.2", "http_port": 9100, "http_path": "/x"},
    )

    runner.invoke(app, ["serve", "--transport", "http"])
    assert (spy_serve["host"], spy_serve["port"], spy_serve["path"]) == (
        "127.0.0.2", 9100, "/x",
    )


def test_unknown_transport_exits_nonzero():
    result = runner.invoke(app, ["serve", "--transport", "carrier-pigeon"])

    assert result.exit_code == 1
    assert "Unknown transport" in result.output


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True), ("localhost", True), ("::1", True),
        ("127.0.0.5", True), ("0.0.0.0", False), ("192.168.1.10", False),
        ("vault.internal.example", False),  # unresolvable here — must not assume safe
    ],
)
def test_loopback_classification(host, expected):
    assert _is_loopback(host) is expected


# ── register_mcp ─────────────────────────────────────────────────────────────

def test_register_writes_stdio_entry_by_default(tmp_path, monkeypatch):
    claude_json = tmp_path / ".claude.json"
    import archiver_rag.mcp.register as reg

    monkeypatch.setattr(reg, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: type("R", (), {"stdout": "/usr/local/bin/archiver-rag\n"})(),
    )

    reg.register_mcp()

    entry = json.loads(claude_json.read_text())["mcpServers"]["archiver-rag"]
    assert entry == {"command": "/usr/local/bin/archiver-rag", "args": ["serve"]}


def test_register_writes_http_entry_when_given_a_url(tmp_path, monkeypatch):
    claude_json = tmp_path / ".claude.json"
    import archiver_rag.mcp.register as reg

    monkeypatch.setattr(reg, "CLAUDE_JSON", claude_json)

    reg.register_mcp(url="http://127.0.0.1:8077/mcp", name="archiver-rag-http")

    servers = json.loads(claude_json.read_text())["mcpServers"]
    assert servers["archiver-rag-http"] == {
        "type": "http", "url": "http://127.0.0.1:8077/mcp",
    }


def test_register_preserves_other_servers(tmp_path, monkeypatch):
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"obsidian": {"command": "npx"}}}))
    import archiver_rag.mcp.register as reg

    monkeypatch.setattr(reg, "CLAUDE_JSON", claude_json)

    reg.register_mcp(url="http://127.0.0.1:8077/mcp", name="archiver-rag-http")

    servers = json.loads(claude_json.read_text())["mcpServers"]
    assert "obsidian" in servers and "archiver-rag-http" in servers
