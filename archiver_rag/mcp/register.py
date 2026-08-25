import json
import subprocess
from pathlib import Path
from rich import print

CLAUDE_JSON = Path.home() / ".claude.json"


def register_mcp(url: str | None = None, name: str = "archiver-rag"):
    """Write an MCP server entry into ~/.claude.json.

    Default (url=None) writes the stdio entry: Claude Code spawns `archiver-rag serve`
    itself. Pass `url` to register an already-running HTTP server instead — the two
    shapes can coexist under different names, which is the point of the `name` argument.
    """
    config = {}
    if CLAUDE_JSON.exists():
        config = json.loads(CLAUDE_JSON.read_text())

    config.setdefault("mcpServers", {})
    if url:
        config["mcpServers"][name] = {"type": "http", "url": url}
    else:
        exe = subprocess.run(
            ["which", "archiver-rag"], capture_output=True, text=True
        ).stdout.strip()
        config["mcpServers"][name] = {"command": exe, "args": ["serve"]}

    CLAUDE_JSON.write_text(json.dumps(config, indent=2))
    print(f"[green]Registered {name} MCP in Claude Code[/green]")
