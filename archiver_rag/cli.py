import typer
from pathlib import Path
from rich import print

app = typer.Typer(
    name="archiver-rag",
    help="Semantic RAG for Obsidian vaults + MCP"
)

@app.command()
def init():
    """Interactive setup wizard — run this first"""
    from archiver_rag.init_cmd import run_init
    run_init()

@app.command()
def start():
    """Start the vault watcher service"""
    from archiver_rag.service import start as svc_start
    svc_start()

@app.command()
def stop():
    """Stop the vault watcher service"""
    from archiver_rag.service import stop as svc_stop
    svc_stop()

@app.command()
def restart():
    """Restart the vault watcher service"""
    from archiver_rag.service import stop as svc_stop, start as svc_start
    svc_stop()
    svc_start()

@app.command()
def status():
    """Check if the watcher service is running"""
    from archiver_rag.service import status as svc_status
    svc_status()

@app.command()
def index(vault_path: str = typer.Argument(None, help="Path to vault (uses config if omitted)")):
    """Force re-index the entire vault"""
    from archiver_rag.core.ingest import ingest_vault
    from archiver_rag.init_cmd import load_config
    path = vault_path or load_config()["vault_path"]
    print(f"[yellow]Indexing {path}...[/yellow]")
    ingest_vault(path)
    print("[green]✅ Done![/green]")

@app.command()
def sync(vault_path: str = typer.Argument(None, help="Path to vault (uses config if omitted)")):
    """Sync only new or modified notes — faster than index"""
    from archiver_rag.core.ingest import sync_vault
    from archiver_rag.init_cmd import load_config
    path = vault_path or load_config()["vault_path"]
    print(f"[yellow]Syncing {path}...[/yellow]")
    result = sync_vault(path)
    print(
        f"[green]✅ Sync complete:[/green] "
        f"[bold]{result['indexed']}[/bold] ingested, "
        f"[dim]{result['up_to_date']} up-to-date[/dim]"
    )

@app.command()
def prune(vault_path: str = typer.Argument(None, help="Path to vault (uses config if omitted)")):
    """Remove index chunks whose source file no longer exists on disk"""
    from archiver_rag.core.ingest import prune_orphans
    from archiver_rag.init_cmd import load_config
    path = vault_path or load_config()["vault_path"]
    count = prune_orphans(path)
    if count:
        print(f"[green]✅ Pruned {count} orphaned source(s)[/green]")
    else:
        print("[dim]No orphans found[/dim]")


@app.command()
def search(query: str = typer.Argument(..., help="Search query")):
    """Search the vault index directly"""
    from archiver_rag.core.embedder import embed
    from archiver_rag.core.db import collection
    import json

    query_vector = embed([query])[0]
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=3,
        include=["documents", "metadatas", "distances"]
    )

    docs = results["documents"][0]
    if not docs:
        print("[red]No results found[/red]")
        return

    for doc, meta, dist in zip(docs, results["metadatas"][0], results["distances"][0]):
        score = round(1 - (dist / 2), 3)
        print(f"\n[green]Score: {score}[/green] — [blue]{meta['source']}[/blue]")
        print(doc[:300])

@app.command()
def health():
    """Check how many chunks are in the index"""
    from archiver_rag.core.db import collection
    count = collection.count()
    print(f"[green]Total chunks in index: {count}[/green]")
    if count > 0:
        results = collection.peek(limit=3)
        for doc, meta in zip(results["documents"], results["metadatas"]):
            print(f"\n[blue]{meta['source']}[/blue]")
            print(doc[:200])

@app.command()
def logs():
    """Tail the service logs"""
    import subprocess
    subprocess.run(["tail", "-f", "/tmp/archiver-rag.log"])

@app.command()
def uninstall():
    """Remove all archiver-rag data, service, and MCP registration"""
    from rich.prompt import Confirm
    import json
    import subprocess
    import sys

    confirm = Confirm.ask(
        "[red]This will remove all config, vectors, and service registration. Continue?[/red]",
        default=False
    )
    if not confirm:
        print("Aborted.")
        raise typer.Exit()

    # 1. Stop and remove service
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.archiver-rag.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink()
            print("[yellow]Service removed[/yellow]")

    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "archiver-rag"], capture_output=True)
        service = Path.home() / ".config/systemd/user/archiver-rag.service"
        if service.exists():
            service.unlink()
        print("[yellow]Service removed[/yellow]")

    # 2. Remove MCP from ~/.claude.json
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        config = json.loads(claude_json.read_text())
        config.get("mcpServers", {}).pop("archiver-rag", None)
        claude_json.write_text(json.dumps(config, indent=2))
        print("[yellow]🔌 MCP entry removed from Claude Code[/yellow]")

    # 3. Remove data directory
    import shutil
    data_dir = Path.home() / ".archiver-rag"
    if data_dir.exists():
        shutil.rmtree(data_dir)
        print("[yellow]🗑  Data directory removed[/yellow]")

    print("\n[green]Uninstall complete.[/green]")
    print("Run [bold]pip uninstall archiver-rag[/bold] to remove the package itself.")

@app.command(name="log")
def log_cmd(
    title: str = typer.Argument(..., help="Note title"),
    type: str = typer.Option("note", "--type", "-t", help="Note category (becomes folder)"),
    tags: list[str] = typer.Option([], "--tag", help="Tags (repeatable)"),
    related: list[str] = typer.Option([], "--related", help="Related note names (repeatable)"),
):
    """Create a knowledge note (opens editor for content)"""
    import click
    from archiver_rag.vault.notes import log_note as _log_note

    content = click.edit("") or ""
    if not content.strip():
        print("[yellow]No content — note not created[/yellow]")
        raise typer.Exit()

    result = _log_note(title=title, content=content.strip(), type=type, tags=tags, related_notes=related)
    print(f"[green]✅ Created:[/green] {result['created']}")


@app.command()
def cluster(
    apply: bool = typer.Option(False, "--apply", help="Move files automatically"),
    min_size: int = typer.Option(2, "--min-size", help="Minimum cluster size"),
):
    """Suggest folder groupings via label propagation"""
    from archiver_rag.graph.clustering import cluster_vault as _cluster_vault, apply_clusters
    result = _cluster_vault(min_cluster_size=min_size)
    print(f"\n[bold]Found {result['total_clusters']} clusters across {result['total_notes']} notes[/bold]")
    for c in result["clusters"]:
        print(f"\n[green]{c['name']}[/green] ({c['size']} notes) → [blue]{c['suggested_folder']}/[/blue]")
        for note in c["notes"]:
            print(f"  {note}")
    if result["unclustered"]:
        print(f"\n[yellow]Unclustered ({len(result['unclustered'])}):[/yellow]")
        for stem in result["unclustered"]:
            print(f"  {stem}")
    if apply:
        moves = apply_clusters(result["clusters"])
        print(f"\n[green]✅ Applied {len(moves)} move operation(s)[/green]")
    else:
        print("\n[dim]Run with --apply to move files[/dim]")


@app.command()
def place(
    note: str = typer.Argument(..., help="Note filename e.g. AuditTrail.md"),
    apply: bool = typer.Option(False, "--apply", help="Move the note"),
):
    """Suggest or apply folder placement for a single note"""
    from archiver_rag.graph.clustering import cluster_note as _cluster_note
    from archiver_rag.vault.reorganize import move_notes
    result = _cluster_note(note)
    if result["suggested_folder"]:
        print(f"\n[green]Suggested:[/green] {result['suggested_folder']}/")
        print(f"{result['reason']}")
        if apply:
            stem = Path(note).stem
            from archiver_rag.utils import get_vault_path
            vault = Path(get_vault_path())
            found = list(vault.rglob(f"{stem}.md"))
            if found:
                src = str(found[0].relative_to(vault))
                dst = f"{result['suggested_folder']}/{Path(note).name}"
                move_notes([{"source": src, "destination": dst}])
                print(f"[green]✅ Moved to {dst}[/green]")
    else:
        print(f"[yellow]No suggestion: {result['reason']}[/yellow]")


@app.command(name="config")
def config_cmd(
    auto_cluster: bool = typer.Option(None, "--auto-cluster/--no-auto-cluster", help="Enable watcher auto-clustering"),
    cluster_threshold: int = typer.Option(None, "--cluster-threshold", help="New notes before full re-cluster"),
):
    """Update archiver-rag configuration"""
    import json
    from archiver_rag.init_cmd import load_config, CONFIG_PATH
    cfg = load_config()
    if auto_cluster is not None:
        cfg["auto_cluster"] = auto_cluster
    if cluster_threshold is not None:
        cfg["cluster_threshold"] = cluster_threshold
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print("[green]✅ Config updated[/green]")


@app.command(name="serve", hidden=True)
def serve():
    """Internal — runs the MCP server"""
    import asyncio
    from archiver_rag.mcp.server import main
    asyncio.run(main())

@app.command(name="watch", hidden=True)
def watch(vault_path: str = typer.Argument(...)):
    """Internal — runs the watcher (called by the service)"""
    from archiver_rag.watcher import watch
    watch(vault_path)

if __name__ == "__main__":
    app()
