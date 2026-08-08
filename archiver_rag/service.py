import subprocess
import sys
from pathlib import Path
from rich import print
from archiver_rag.init_cmd import load_config

LABEL = "com.archiver-rag"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


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
    <string>/tmp/archiver-rag.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/archiver-rag.error.log</string>
</dict>
</plist>"""
        PLIST_PATH.write_text(plist)
        subprocess.run(["launchctl", "load", str(PLIST_PATH)])
        print("[green]✅ Service registered with launchd[/green]")

    elif sys.platform.startswith("linux"):
        service_path = Path.home() / ".config/systemd/user/archiver-rag.service"
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


def status():
    if sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "list", LABEL], capture_output=True, text=True
        )
        if result.returncode == 0:
            print("[green]✅ Running[/green]")
        else:
            print("[red]❌ Not running[/red]")
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "status", "archiver-rag"])
