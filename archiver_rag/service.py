import re
import subprocess
import sys
from pathlib import Path
from rich import print
from archiver_rag.init_cmd import load_config

LABEL = "com.archiver-rag"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
UNIT_PATH = Path.home() / ".config/systemd/user/archiver-rag.service"
# Interpolated into the plist below *and* reported by service_state(), so the paths
# `status` prints cannot drift from the ones launchd actually writes to.
STDOUT_LOG = "/tmp/archiver-rag.log"
STDERR_LOG = "/tmp/archiver-rag.error.log"

# launchctl list <label> prints an old-style plist dict: `"PID" = 17249;`
_LAUNCHCTL_KEY_RE = re.compile(r'^\s*"(PID|LastExitStatus)"\s*=\s*(-?\d+);', re.MULTILINE)


def _get_exe():
    result = subprocess.run(["which", "archiver-rag"], capture_output=True, text=True)
    return result.stdout.strip()


def setup_service():
    config = load_config()
    exe = _get_exe()

    if sys.platform == "darwin":
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>watch</string>
        <string>{config["vault_path"]}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{STDOUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>{STDERR_LOG}</string>
</dict>
</plist>"""
        PLIST_PATH.write_text(plist)
        subprocess.run(["launchctl", "load", str(PLIST_PATH)])
        print("[green]✅ Service registered with launchd[/green]")

    elif sys.platform.startswith("linux"):
        service_path = UNIT_PATH
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service = f"""[Unit]
Description=Archiver RAG Watcher
After=network.target

[Service]
ExecStart={exe} watch {config["vault_path"]}
Restart=always
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target"""
        service_path.write_text(service)
        subprocess.run(["systemctl", "--user", "enable", "--now", "archiver-rag"])
        print("[green]✅ Service registered with systemd[/green]")


def start():
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "load", str(PLIST_PATH)])
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "start", "archiver-rag"])
    print("[green]✅ Service started[/green]")


def stop():
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)])
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "stop", "archiver-rag"])
    print("[yellow]🛑 Service stopped[/yellow]")


def _darwin_state() -> dict:
    """Parse `launchctl list com.archiver-rag`.

    A loaded job that keeps dying reports no PID but does report LastExitStatus, and
    launchctl still exits 0 — so returncode alone (all the old status() looked at)
    renders a crash-loop as "✅ Running". Requiring a PID is what makes that visible.
    """
    result = subprocess.run(
        ["launchctl", "list", LABEL], capture_output=True, text=True
    )
    fields = {k: int(v) for k, v in _LAUNCHCTL_KEY_RE.findall(result.stdout)}
    pid = fields.get("PID")
    return {
        "platform": "darwin",
        "installed": PLIST_PATH.exists(),
        "loaded": result.returncode == 0,
        "running": result.returncode == 0 and pid is not None,
        "pid": pid,
        "last_exit_status": fields.get("LastExitStatus"),
        "service_file": str(PLIST_PATH),
        "stdout_log": STDOUT_LOG,
        "stderr_log": STDERR_LOG,
    }


def _linux_state() -> dict:
    """Parse `systemctl --user show` — machine-readable, unlike `systemctl status`."""
    result = subprocess.run(
        [
            "systemctl", "--user", "show", "archiver-rag",
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
        "installed": UNIT_PATH.exists(),
        "loaded": fields.get("ActiveState") not in (None, "inactive", "failed"),
        "running": active and pid is not None,
        "pid": pid,
        "last_exit_status": exit_status,
        "service_file": str(UNIT_PATH),
        "stdout_log": "journalctl --user -u archiver-rag",
        "stderr_log": "journalctl --user -u archiver-rag",
    }


def service_state() -> dict:
    """Structured service liveness. Never raises — an unknown platform reports not-installed."""
    try:
        if sys.platform == "darwin":
            return _darwin_state()
        if sys.platform.startswith("linux"):
            return _linux_state()
    except Exception as e:
        return {
            "platform": sys.platform,
            "installed": False,
            "loaded": False,
            "running": False,
            "pid": None,
            "last_exit_status": None,
            "service_file": None,
            "stdout_log": STDOUT_LOG,
            "stderr_log": STDERR_LOG,
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "platform": sys.platform,
        "installed": False,
        "loaded": False,
        "running": False,
        "pid": None,
        "last_exit_status": None,
        "service_file": None,
        "stdout_log": STDOUT_LOG,
        "stderr_log": STDERR_LOG,
        "error": f"unsupported platform: {sys.platform}",
    }
