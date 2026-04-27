import json
import typer
import subprocess
from rich import print
from pathlib import Path
from rich.prompt import Prompt, Confirm
from archiver_rag.core.embedder import _is_cached

CONFIG_PATH = Path.home() / ".archiver-rag" / "config.json"

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("[red]Not initialized. Run `archiver-rag init` first.[/red]")
        raise typer.Exit(1)
    return json.loads(CONFIG_PATH.read_text())

def run_init():
    print("[blue]🔍 Archiver RAG Setup[/blue]\n")

    # 1. Ask for vault path
    vault_path = Prompt.ask("Path to your Obsidian vault")
    if not Path(vault_path).exists():
        print("[red]Path does not exist[/red]")
        raise typer.Exit(1)

    install_path = Path.home() / ".archiver-rag"
    install_path.mkdir(parents=True, exist_ok=True)
    chroma_path = install_path / "chroma_db"

    auto_start = Confirm.ask("Start automatically on login?", default=True)

    # 2. Save config
    config = {
        "vault_path": str(vault_path),
        "install_path": str(install_path),
        "chroma_path": str(chroma_path),
        "auto_cluster": True,
        "cluster_threshold": 5,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print("[green]✅ Config saved[/green]")

    # 3. Check for embedding model
    if not _is_cached():
        print("[yellow]📥 Downloading embedding model (~90MB, first time only)...[/yellow]")
    else:
        print("[green]✅ Embedding model found in cache[/green]")

    # 4. Run initial index
    print("\n[yellow]📚 Indexing vault...[/yellow]")
    from archiver_rag.core.ingest import ingest_vault
    ingest_vault(vault_path)

    # 5. Register MCP in Claude Code
    from archiver_rag.mcp.register import register_mcp
    register_mcp()

    # 6. Setup service
    if auto_start:
        from archiver_rag.service import setup_service
        setup_service()

    print("\n[green]✅ Setup complete![/green]")
    print("Run [bold]archiver-rag status[/bold] to verify.")
