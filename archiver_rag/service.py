"""Daemon lifecycle for both long-running processes: the vault watcher and the
detached MCP HTTP server.

One abstraction, two definitions (`WATCHER`, `HTTP`): each carries its platform
paths, its launchd/systemd name, and its log destinations, so start/stop/state
cannot mix one service's plist with the other's label. The watcher's values are
exactly what this module hardcoded before generalization.

Liveness comes from the supervisor, never from a PID file we maintain: launchd
and systemd *are* the source of truth, and requiring a PID before reporting
running is what distinguishes a healthy process from a KeepAlive crash-loop
(`launchctl list` exits 0 either way).
"""

import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from rich import print
from archiver_rag.init_cmd import load_config

_LAUNCHCTL_KEY_RE = re.compile(r'^\s*"(PID|LastExitStatus)"\s*=\s*(-?\d+);', re.MULTILINE)


@dataclass(frozen=True)
class ServiceDef:
    """Everything that differs between the two daemons."""

    label: str  # launchd label == systemd unit base name
    plist_path: Path  # macOS
    unit_path: Path  # Linux
    stdout_log: str
    stderr_log: str

    @property
    def unit_name(self) -> str:
        # com.archiver-rag → archiver-rag (the pre-existing unit name);
        # com.archiver-rag.http → archiver-rag-http.
        return self.label.removeprefix("com.").replace(".", "-")


WATCHER = ServiceDef(
    label="com.archiver-rag",
    plist_path=Path.home() / "Library/LaunchAgents/com.archiver-rag.plist",
    unit_path=Path.home() / ".config/systemd/user/archiver-rag.service",
    stdout_log="/tmp/archiver-rag.log",
    stderr_log="/tmp/archiver-rag.error.log",
)

HTTP = ServiceDef(
    label="com.archiver-rag.http",
    plist_path=Path.home() / "Library/LaunchAgents/com.archiver-rag.http.plist",
    unit_path=Path.home() / ".config/systemd/user/archiver-rag-http.service",
    stdout_log="/tmp/archiver-rag-http.log",
    stderr_log="/tmp/archiver-rag-http.error.log",
)


def _get_exe():
    result = subprocess.run(["which", "archiver-rag"], capture_output=True, text=True)
    return result.stdout.strip()


def write_service(defn: ServiceDef, args: list[str], *, run_at_load: bool) -> None:
    """Install (or refresh) the plist/unit for `defn`, running `exe args`.

    The file is rewritten every time, so flags baked into `args` persist until
    the next write — matching launchd semantics, where the plist is the record.
    """
    exe = _get_exe()
    run_at_load_key = "<true/>" if run_at_load else "<false/>"

    if sys.platform == "darwin":
        program_args = "\n".join(f"\t\t<string>{a}</string>" for a in [exe, *args])
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{defn.label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    {run_at_load_key}
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{defn.stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{defn.stderr_log}</string>
</dict>
</plist>"""
        defn.plist_path.write_text(plist)

    elif sys.platform.startswith("linux"):
        defn.unit_path.parent.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join([exe, *args])
        unit = f"""[Unit]
Description=Archiver RAG {defn.unit_name}

[Service]
ExecStart={exec_start}
Restart=always
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target"""
        defn.unit_path.write_text(unit)

    else:
        raise RuntimeError(f"unsupported platform: {sys.platform}")


def setup_service():
    """Watcher-only, called by `init`: install + load with RunAtLoad."""
    config = load_config()

    if sys.platform == "darwin":
        write_service(
            WATCHER, ["watch", config["vault_path"]], run_at_load=True
        )
        subprocess.run(["launchctl", "load", str(WATCHER.plist_path)])
        print("[green]✅ Service registered with launchd[/green]")

    elif sys.platform.startswith("linux"):
        write_service(
            WATCHER, ["watch", config["vault_path"]], run_at_load=True
        )
        subprocess.run(["systemctl", "--user", "enable", "--now", WATCHER.unit_name])
        print("[green]✅ Service registered with systemd[/green]")


def start(defn: ServiceDef = WATCHER) -> None:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "load", str(defn.plist_path)])
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "start", defn.unit_name])
    else:
        raise RuntimeError(f"unsupported platform: {sys.platform}")
    print("[green]✅ Service started[/green]")


def stop(defn: ServiceDef = WATCHER) -> None:
    """Unload/stop. The plist/unit file stays on disk, so settings survive."""
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", str(defn.plist_path)])
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "stop", defn.unit_name])
    else:
        raise RuntimeError(f"unsupported platform: {sys.platform}")
    print("[yellow]🛑 Service stopped[/yellow]")


def port_in_use(host: str, port: int) -> bool:
    """True when something accepts connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _darwin_state(defn: ServiceDef) -> dict:
    """Parse `launchctl list <label>`.

    A loaded job that keeps dying reports no PID but does report LastExitStatus, and
    launchctl still exits 0 — so returncode alone (all the old status() looked at)
    renders a crash-loop as "✅ Running". Requiring a PID is what makes that visible.
    """
    result = subprocess.run(
        ["launchctl", "list", defn.label], capture_output=True, text=True
    )
    fields = {k: int(v) for k, v in _LAUNCHCTL_KEY_RE.findall(result.stdout)}
    pid = fields.get("PID")
    return {
        "platform": "darwin",
        "label": defn.label,
        "installed": defn.plist_path.exists(),
        "loaded": result.returncode == 0,
        "running": result.returncode == 0 and pid is not None,
        "pid": pid,
        "last_exit_status": fields.get("LastExitStatus"),
        "service_file": str(defn.plist_path),
        "stdout_log": defn.stdout_log,
        "stderr_log": defn.stderr_log,
    }


def _linux_state(defn: ServiceDef) -> dict:
    """Parse `systemctl --user show` — machine-readable, unlike `systemctl status`."""
    result = subprocess.run(
        [
            "systemctl", "--user", "show", defn.unit_name,
            "--property=ActiveState", "--property=MainPID",
            "--property=ExecMainStatus",
        ],
        capture_output=True,
        text=True,
    )
    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    try:
        pid = int(fields.get("MainPID", "0")) or None
    except ValueError:
        pid = None
    try:
        exit_status = int(fields.get("ExecMainStatus", ""))
    except ValueError:
        exit_status = None
    active = fields.get("ActiveState") == "active"
    return {
        "platform": "linux",
        "label": defn.label,
        "installed": defn.unit_path.exists(),
        "loaded": fields.get("ActiveState") not in (None, "inactive", "failed"),
        "running": active and pid is not None,
        "pid": pid,
        "last_exit_status": exit_status,
        "service_file": str(defn.unit_path),
        "stdout_log": f"journalctl --user -u {defn.unit_name}",
        "stderr_log": f"journalctl --user -u {defn.unit_name}",
    }


def _state_unsupported(defn: ServiceDef, error: str) -> dict:
    return {
        "platform": sys.platform,
        "label": defn.label,
        "installed": False,
        "loaded": False,
        "running": False,
        "pid": None,
        "last_exit_status": None,
        "service_file": None,
        "stdout_log": defn.stdout_log,
        "stderr_log": defn.stderr_log,
        "error": error,
    }


def state(defn: ServiceDef) -> dict:
    """Structured liveness for one service. Never raises — an unknown platform or a
    failing supervisor command reports rather than propagates."""
    try:
        if sys.platform == "darwin":
            return _darwin_state(defn)
        if sys.platform.startswith("linux"):
            return _linux_state(defn)
    except Exception as e:
        return _state_unsupported(defn, f"{type(e).__name__}: {e}")
    return _state_unsupported(defn, f"unsupported platform: {sys.platform}")


def service_state() -> dict:
    """Watcher liveness — the pre-generalization entry point, kept for callers
    (relink, report) that mean the watcher specifically."""
    return state(WATCHER)


def http_state() -> dict:
    return state(HTTP)


def baked_program_args(defn: ServiceDef) -> list[str] | None:
    """Arguments (sans executable) recorded in the installed service file.

    `start http` bakes explicit --host/--port/--path flags into the plist/unit, so
    between rewrites the daemon's actual bind address lives there, not in config.
    Returns None when not installed, nothing recorded, or anything fails to parse —
    callers fall back to the configured endpoint.
    """
    try:
        if sys.platform == "darwin":
            if not defn.plist_path.exists():
                return None
            import plistlib

            data = plistlib.loads(defn.plist_path.read_bytes())
            args = [str(a) for a in data.get("ProgramArguments", [])]
            return args[1:] if len(args) > 1 else None
        if sys.platform.startswith("linux"):
            if not defn.unit_path.exists():
                return None
            for line in defn.unit_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("ExecStart="):
                    parts = line.split("=", 1)[1].split()
                    return parts[1:]
            return None
    except Exception:
        return None
    return None


def daemon_endpoint() -> tuple[str, str, int, str]:
    """`(url, host, port, path)` of the detached HTTP server as it will actually run.

    Config first (`mcp/http.py::configured_endpoint`), then any --host/--port/--path
    baked into the installed service file win. `status` and the start/stop messaging
    go through this so they describe the daemon by construction — including the
    window between an overridden `start http` and the next plain rewrite.
    """
    from archiver_rag.mcp.http import configured_endpoint

    url, host, port, path = configured_endpoint()
    args = baked_program_args(HTTP) or []

    def _flag(flag: str, default: str) -> str:
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    host = _flag("--host", host)
    path = _flag("--path", path)
    try:
        port = int(_flag("--port", str(port)))
    except ValueError:
        pass
    return f"http://{host}:{port}{path}", host, port, path
