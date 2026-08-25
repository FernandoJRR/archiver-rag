"""Detached MCP HTTP server lifecycle: service definition, install, port pre-flight,
and the `start|stop|restart http` CLI surface.

Everything runs through monkeypatched subprocess and tmp-path ServiceDefs — no test
loads a real supervisor, writes a real plist, or touches ~/.claude.json.
"""

from __future__ import annotations

import socket
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archiver_rag import service
from archiver_rag.cli import app

runner = CliRunner()


@pytest.fixture
def http_def(tmp_path, monkeypatch):
    """HTTP ServiceDef rooted in tmp_path, patched into the module so the CLI
    operates on it instead of the real LaunchAgents directory."""
    defn = service.ServiceDef(
        label="com.archiver-rag.http",
        plist_path=tmp_path / "com.archiver-rag.http.plist",
        unit_path=tmp_path / "archiver-rag-http.service",
        stdout_log=str(tmp_path / "http.out.log"),
        stderr_log=str(tmp_path / "http.err.log"),
    )
    monkeypatch.setattr(service, "HTTP", defn)
    return defn


@pytest.fixture
def fake_exe(monkeypatch):
    monkeypatch.setattr(service, "_get_exe", lambda: "/fake/bin/archiver-rag")


@pytest.fixture
def recorded_run(monkeypatch):
    """subprocess.run recorder that answers `which` and launchctl list sensibly."""
    calls: list[list[str]] = []

    def _run(cmd, *a, **kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        stdout = ""
        if cmd[:1] == ["which"]:
            stdout = "/fake/bin/archiver-rag\n"
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _plist_text(defn) -> str:
    return defn.plist_path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# write_service
# ──────────────────────────────────────────────────────────────────────────────


def test_darwin_plist_carries_label_args_logs_and_keepalive(
    monkeypatch, http_def, fake_exe
):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    service.write_service(http_def, ["serve", "--transport", "http"], run_at_load=True)

    text = _plist_text(http_def)
    assert f"<string>{http_def.label}</string>" in text
    assert "<string>/fake/bin/archiver-rag</string>" in text
    assert "<string>--transport</string>" in text
    assert "<string>http</string>" in text
    assert "<key>RunAtLoad</key>\n    <true/>" in text
    assert "<key>KeepAlive</key>\n    <true/>" in text
    assert http_def.stdout_log in text
    assert http_def.stderr_log in text


def test_darwin_run_at_load_false_when_not_requested(monkeypatch, http_def, fake_exe):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    service.write_service(http_def, ["serve"], run_at_load=False)

    assert "<key>RunAtLoad</key>\n    <false/>" in _plist_text(http_def)


def test_linux_unit_exec_start_joins_args(monkeypatch, http_def, fake_exe):
    monkeypatch.setattr(service.sys, "platform", "linux")
    args = ["serve", "--transport", "http", "--allowed-host", "mcp.example.com"]
    service.write_service(http_def, args, run_at_load=False)

    text = http_def.unit_path.read_text(encoding="utf-8")
    assert f"ExecStart=/fake/bin/archiver-rag {' '.join(args)}" in text
    assert "Restart=always" in text


def test_write_service_rejects_unknown_platform(monkeypatch, http_def, fake_exe):
    monkeypatch.setattr(service.sys, "platform", "sunos5")
    with pytest.raises(RuntimeError, match="unsupported platform"):
        service.write_service(http_def, ["serve"], run_at_load=False)


# ──────────────────────────────────────────────────────────────────────────────
# port_in_use
# ──────────────────────────────────────────────────────────────────────────────


def test_port_in_use_true_when_a_listener_accepts():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        host, port = srv.getsockname()
        assert service.port_in_use(host, port) is True


def test_port_in_use_false_on_a_free_ephemeral_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
    # Socket closed above — nothing is listening anymore.
    assert service.port_in_use("127.0.0.1", port) is False


# ──────────────────────────────────────────────────────────────────────────────
# start http
# ──────────────────────────────────────────────────────────────────────────────


def test_start_http_happy_path_bakes_login_and_loads(
    monkeypatch, http_def, fake_exe, recorded_run, tmp_path
):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    # The pre-flight must not depend on this machine's actual port state.
    monkeypatch.setattr(service, "port_in_use", lambda host, port: False)

    result = runner.invoke(app, ["start", "http", "--login"])

    assert result.exit_code == 0, result.output
    text = _plist_text(http_def)
    assert "--stateful" not in text  # default is stateless
    assert "<true/>" in text  # --login baked into RunAtLoad
    loads = [c for c in recorded_run if c[:2] == ["launchctl", "load"]]
    assert [str(http_def.plist_path)] == [c[-1] for c in loads]
    # Decision 2: registration stays manual — print the URL and how to add it.
    assert "http://127.0.0.1:8077/mcp" in result.output
    assert "claude mcp add" in result.output


def test_start_http_bakes_explicit_host_port_overrides(
    monkeypatch, http_def, fake_exe, recorded_run
):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    # Only the configured default is busy — proving the pre-flight probes the
    # overridden port, and that the override then proceeds.
    monkeypatch.setattr(service, "port_in_use", lambda host, port: port != 8088)

    result = runner.invoke(app, ["start", "http", "--login", "--port", "8088"])

    assert result.exit_code == 0, result.output
    text = _plist_text(http_def)
    assert "<string>--port</string>" in text
    assert "<string>8088</string>" in text
    # Pre-flight probed the overridden port, not the configured one.
    assert "http://127.0.0.1:8088/mcp" in result.output


def test_start_http_prompt_defaults_to_not_persistent(
    monkeypatch, http_def, fake_exe, recorded_run
):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "port_in_use", lambda host, port: False)

    result = runner.invoke(app, ["start", "http"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Start automatically at login?" in result.output
    assert "<false/>" in _plist_text(http_def)


def test_start_http_already_running_short_circuits(monkeypatch, http_def, recorded_run):
    monkeypatch.setattr(
        service,
        "http_state",
        lambda: {"running": True, "pid": 4321},
    )

    result = runner.invoke(app, ["start", "http"])

    assert result.exit_code == 0, result.output
    assert "already running" in result.output
    # Nothing was loaded or written — idempotent.
    assert not any(c[:2] == ["launchctl", "load"] for c in recorded_run)
    assert not http_def.plist_path.exists()


def test_start_http_port_busy_aborts_before_loading(monkeypatch, http_def, recorded_run):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "port_in_use", lambda host, port: True)

    result = runner.invoke(app, ["start", "http"])

    assert result.exit_code == 1
    assert "already in use" in result.output
    # The whole point of the pre-flight: no plist written, nothing loaded — loading
    # into a busy port would KeepAlive-crash-loop.
    assert not http_def.plist_path.exists()
    assert not any(c[:2] == ["launchctl", "load"] for c in recorded_run)


def test_start_http_non_loopback_warns_about_no_auth(
    monkeypatch, http_def, fake_exe, recorded_run
):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(
        "archiver_rag.mcp.http.configured_endpoint",
        lambda: ("http://0.0.0.0:8077/mcp", "0.0.0.0", 8077, "/mcp"),
    )
    # Don't let the pre-flight depend on whether this machine happens to run
    # something on 8077 — the test is about the warning, not the port probe.
    monkeypatch.setattr(service, "port_in_use", lambda host, port: False)

    result = runner.invoke(app, ["start", "http", "--login"])

    assert result.exit_code == 0, result.output
    assert "NO authentication" in result.output


# ──────────────────────────────────────────────────────────────────────────────
# stop / restart / target validation
# ──────────────────────────────────────────────────────────────────────────────


def test_stop_http_unloads_the_http_plist(monkeypatch, http_def, fake_exe, recorded_run):
    monkeypatch.setattr(service.sys, "platform", "darwin")

    result = runner.invoke(app, ["stop", "http"])

    assert result.exit_code == 0, result.output
    unloads = [c for c in recorded_run if c[:2] == ["launchctl", "unload"]]
    assert [str(http_def.plist_path)] == [c[-1] for c in unloads]


def test_stop_defaults_to_watcher_not_http(monkeypatch, http_def, fake_exe, recorded_run):
    watcher = service.ServiceDef(
        label="com.watcher-stop-test",
        plist_path=http_def.plist_path.parent / "watcher.plist",
        unit_path=watcher_unit(http_def),
        stdout_log="/tmp/w.out",
        stderr_log="/tmp/w.err",
    )
    monkeypatch.setattr(service, "WATCHER", watcher)
    monkeypatch.setattr(service.sys, "platform", "darwin")

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    unloads = [c for c in recorded_run if c[:2] == ["launchctl", "unload"]]
    assert [str(watcher.plist_path)] == [c[-1] for c in unloads]


def watcher_unit(http_def) -> Path:
    return http_def.unit_path.parent / "watcher.service"


def test_restart_http_requires_install_first(monkeypatch, http_def, fake_exe, recorded_run):
    monkeypatch.setattr(service.sys, "platform", "darwin")

    result = runner.invoke(app, ["restart", "http"])  # never installed → absent file

    assert result.exit_code == 1
    assert "not installed" in result.output
    # state() itself may call `launchctl list`, but nothing may load or unload.
    assert not any(
        c[0] == "launchctl" and c[1] in ("load", "unload") for c in recorded_run
    )


def test_restart_http_stops_then_starts(monkeypatch, http_def, fake_exe, recorded_run):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    http_def.plist_path.write_text("<plist/>", encoding="utf-8")  # installed

    result = runner.invoke(app, ["restart", "http"])

    assert result.exit_code == 0, result.output
    verbs = [
        (c[1], str(c[-1]))
        for c in recorded_run
        if c[0] == "launchctl" and c[1] in ("unload", "load")
    ]
    assert verbs == [
        ("unload", str(http_def.plist_path)),
        ("load", str(http_def.plist_path)),
    ]


@pytest.mark.parametrize("cmd", ["start", "stop", "restart"])
def test_unknown_target_exits_nonzero_with_options(cmd):
    result = runner.invoke(app, [cmd, "redis"])
    assert result.exit_code == 1
    assert "watcher" in result.output and "http" in result.output


# ──────────────────────────────────────────────────────────────────────────────
# daemon_endpoint — baked flags must be visible to status/start messaging
# ──────────────────────────────────────────────────────────────────────────────


def test_daemon_endpoint_reports_baked_port(monkeypatch, tmp_path):
    """`start http --port N` bakes N into the plist; status must name N, not config."""
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_get_exe", lambda: "/fake/bin/archiver-rag")
    defn = service.ServiceDef(
        label=service.HTTP.label,
        plist_path=tmp_path / "http.plist",
        unit_path=tmp_path / "http.service",
        stdout_log="/tmp/o", stderr_log="/tmp/e",
    )
    monkeypatch.setattr(service, "HTTP", defn)
    service.write_service(
        defn, ["serve", "--transport", "http", "--port", "8099"], run_at_load=False
    )

    url, host, port, path = service.daemon_endpoint()
    assert port == 8099
    assert url == "http://127.0.0.1:8099/mcp"


def test_daemon_endpoint_falls_back_to_config_when_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    defn = service.ServiceDef(
        label=service.HTTP.label,
        plist_path=tmp_path / "absent.plist",
        unit_path=tmp_path / "absent.service",
        stdout_log="/tmp/o", stderr_log="/tmp/e",
    )
    monkeypatch.setattr(service, "HTTP", defn)

    url, host, port, path = service.daemon_endpoint()
    # No service file → nothing baked → the configured loopback defaults.
    assert (url, host, port, path) == (
        "http://127.0.0.1:8077/mcp", "127.0.0.1", 8077, "/mcp"
    )


def test_daemon_endpoint_reads_linux_exec_start(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "linux")
    defn = service.ServiceDef(
        label=service.HTTP.label,
        plist_path=tmp_path / "h.plist",
        unit_path=tmp_path / "h.service",
        stdout_log="/tmp/o", stderr_log="/tmp/e",
    )
    monkeypatch.setattr(service, "HTTP", defn)
    defn.unit_path.write_text(
        "[Service]\n"
        f"ExecStart=/fake/bin/archiver-rag serve --transport http --port 9099\n",
        encoding="utf-8",
    )

    url, _h, port, _p = service.daemon_endpoint()
    assert port == 9099 and ":9099" in url


# ──────────────────────────────────────────────────────────────────────────────
# uninstall covers both services
# ──────────────────────────────────────────────────────────────────────────────


def test_uninstall_removes_both_services(monkeypatch, http_def, fake_exe, tmp_path):
    from rich.prompt import Confirm

    monkeypatch.setattr(service.sys, "platform", "darwin")
    watcher = service.ServiceDef(
        label="com.archiver-rag.test",
        plist_path=tmp_path / "w.plist",
        unit_path=tmp_path / "w.service",
        stdout_log="/tmp/w.out",
        stderr_log="/tmp/w.err",
    )
    monkeypatch.setattr(service, "WATCHER", watcher)
    for defn in (watcher, http_def):
        defn.plist_path.write_text("<plist/>", encoding="utf-8")

    # Redirect everything else uninstall touches away from the real machine.
    import archiver_rag.cli as cli

    class _FakeHome:
        def __init__(self, root: Path):
            self._root = root

        @classmethod
        def home(cls):
            return cls(Path("/fake-home"))

        def __truediv__(self, name):
            return self._root / name

    monkeypatch.setattr(cli, "Path", _FakeHome(tmp_path / "home"))
    monkeypatch.setattr(Confirm, "ask", lambda *a, **kw: True)

    result = runner.invoke(app, ["uninstall"])

    assert result.exit_code == 0, result.output
    assert not watcher.plist_path.exists()
    assert not http_def.plist_path.exists()
