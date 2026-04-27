import json
import subprocess
from pathlib import Path
from rich import print

CLAUDE_JSON = Path.home() / ".claude.json"

def register_mcp():
    config = {}
    if CLAUDE_JSON.exists():
        config = json.loads(CLAUDE_JSON.read_text())

    exe = subprocess.run(
        ["which", "archiver-rag"],
        capture_output=True, text=True
    ).stdout.strip()

    config.setdefault("mcpServers", {})
    config["mcpServers"]["archiver-rag"] = {
        "command": exe,
        "args": ["serve"]
    }

    CLAUDE_JSON.write_text(json.dumps(config, indent=2))
    print("[green]Registered archiver-rag MCP in Claude Code[/green]")
