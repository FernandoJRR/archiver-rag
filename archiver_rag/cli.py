import typer
from pathlib import Path
from rich import print

app = typer.Typer(name="archiver-rag", help="Semantic RAG for Obsidian vaults + MCP")


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
def index(
    vault_path: str = typer.Argument(
        None, help="Path to vault (uses config if omitted)"
    ),
):
    """Force re-index the entire vault"""
    from archiver_rag.core.ingest import ingest_vault
    from archiver_rag.init_cmd import load_config

    path = vault_path or load_config()["vault_path"]
    print(f"[yellow]Indexing {path}...[/yellow]")
    ingest_vault(path)
    print("[green]✅ Done![/green]")


@app.command()
def sync(
    vault_path: str = typer.Argument(
        None, help="Path to vault (uses config if omitted)"
    ),
):
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
def prune(
    vault_path: str = typer.Argument(
        None, help="Path to vault (uses config if omitted)"
    ),
):
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
        include=["documents", "metadatas", "distances"],
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
        default=False,
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
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "archiver-rag"],
            capture_output=True,
        )
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
    type: str = typer.Option(
        "note", "--type", "-t", help="Note category (becomes folder)"
    ),
    tags: list[str] = typer.Option([], "--tag", help="Tags (repeatable)"),
    related: list[str] = typer.Option(
        [], "--related", help="Related note names (repeatable)"
    ),
):
    """Create a knowledge note (opens editor for content)"""
    import click
    from archiver_rag.vault.notes import log_note as _log_note

    content = click.edit("") or ""
    if not content.strip():
        print("[yellow]No content — note not created[/yellow]")
        raise typer.Exit()

    result = _log_note(
        title=title,
        content=content.strip(),
        type=type,
        tags=tags,
        related_notes=related,
    )
    print(f"[green]✅ Created:[/green] {result['created']}")


@app.command(name="delete")
def delete_cmd(
    notes: list[str] = typer.Argument(
        ..., help="Notes to delete — relative paths or stems"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Move notes to .trash/ and sweep inbound wikilinks (recoverable)"""
    from archiver_rag.utils import get_vault_path, build_link_map
    from archiver_rag.vault.notes import delete_notes as _delete_notes

    vault = Path(get_vault_path())

    # Resolve each argument to a vault-relative path
    resolved: list[str] = []
    not_found: list[str] = []
    for note_arg in notes:
        candidate = vault / note_arg
        if candidate.exists() and candidate.suffix == ".md":
            resolved.append(note_arg)
        else:
            stem = Path(note_arg).stem
            found = [
                f
                for f in vault.rglob(f"{stem}.md")
                if not any(p.startswith(".") for p in f.relative_to(vault).parts)
            ]
            if found:
                resolved.append(str(found[0].relative_to(vault)))
            else:
                not_found.append(note_arg)

    for n in not_found:
        print(f"[red]Not found:[/red] {n}")
    if not resolved:
        raise typer.Exit(1)

    # Show impact before confirming
    _, incoming = build_link_map(vault)
    stems = [Path(r).stem for r in resolved]
    linker_count = len({linker for stem in stems for linker in incoming.get(stem, [])})

    print("\n[bold]Will move to .trash/ (recoverable):[/bold]")
    for r in resolved:
        print(f"  {r}")
    if linker_count:
        print(
            f"\n[yellow]{linker_count} note(s) link to these — ## Related sections will be swept.[/yellow]"
        )

    if not yes:
        from rich.prompt import Confirm

        confirm = Confirm.ask("\nProceed?", default=False)
        if not confirm:
            print("Aborted.")
            raise typer.Exit()

    result = _delete_notes(resolved)

    for note_rel in result["deleted"]:
        print(f"[green]Trashed:[/green] {note_rel}")
    for swept in result["links_cleaned"]:
        print(f"[dim]  swept links in:[/dim] {swept}")
    for err in result["errors"]:
        src = err.get("source", err.get("file", "?"))
        print(f"[red]Error:[/red] {src} — {err['error']}")

    if result["deleted"]:
        print(f"\n[green]Moved {len(result['deleted'])} note(s) to .trash/[/green]")
    if result["links_cleaned"]:
        print(f"[green]Swept links in {len(result['links_cleaned'])} note(s)[/green]")


@app.command()
def cluster(
    apply: bool = typer.Option(False, "--apply", help="Move files automatically"),
    min_size: int = typer.Option(2, "--min-size", help="Minimum cluster size"),
):
    """Suggest folder groupings via label propagation"""
    from archiver_rag.graph.clustering import (
        cluster_vault as _cluster_vault,
        apply_clusters,
    )

    result = _cluster_vault(min_cluster_size=min_size)
    print(
        f"\n[bold]Found {result['total_clusters']} clusters across {result['total_notes']} notes[/bold]"
    )
    for c in result["clusters"]:
        print(
            f"\n[green]{c['name']}[/green] ({c['size']} notes) → [blue]{c['suggested_folder']}/[/blue]"
        )
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
    note: str = typer.Argument(None, help="Note filename e.g. AuditTrail.md"),
    apply: bool = typer.Option(False, "--apply", help="Move the note(s)"),
    all_notes: bool = typer.Option(False, "--all", help="Report placement for every note (dry-run unless --apply)"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation when using --all --apply"),
):
    """Suggest or apply semantic folder placement.

    Single note: suggest (or move with --apply).
    --all: show current vs suggested folder for every note, with before/after distribution.
    --all --apply: batch move all notes whose suggestion differs from current folder.
    """
    from archiver_rag.graph.placement import suggest_folder
    from archiver_rag.utils import get_vault_path, load_config
    from archiver_rag.vault.reorganize import move_notes

    vault = Path(get_vault_path())
    cfg = load_config()
    threshold = float(cfg.get("placement_similarity_threshold", 0.55))
    type_fb = bool(cfg.get("type_fallback", True))

    if all_notes:
        # ── §9.7 vault-wide dry-run / batch move ────────────────────────────
        from archiver_rag.utils import is_indexable_note

        all_note_paths = [f for f in vault.rglob("*.md") if is_indexable_note(f)]
        if not all_note_paths:
            print("[dim]No notes found.[/dim]")
            return

        rows = []
        for np_ in sorted(all_note_paths):
            suggestion = suggest_folder(vault, np_, threshold=threshold, type_fallback=type_fb)
            try:
                current = str(np_.parent.relative_to(vault))
            except ValueError:
                current = "."
            rows.append({
                "path": str(np_.relative_to(vault)),
                "current": current if current != "." else "(root)",
                "suggested": suggestion["suggested_folder"] or "(none)",
                "similarity": suggestion["similarity"],
                "reason": suggestion["reason"],
                "would_move": suggestion["suggested_folder"] is not None and current != suggestion["suggested_folder"],
            })

        # Current distribution
        from collections import Counter as _Counter
        current_dist = _Counter(r["current"] for r in rows)
        suggested_dist = _Counter(
            r["suggested"] for r in rows if r["suggested"] != "(none)"
        )

        print(f"\n[bold]Placement report — {len(rows)} notes[/bold]\n")
        moves_needed = [r for r in rows if r["would_move"]]
        stays = [r for r in rows if not r["would_move"]]

        if moves_needed:
            print(f"[yellow]Would move ({len(moves_needed)}):[/yellow]")
            for r in moves_needed:
                sim_str = f"{r['similarity']:.2f}" if r["similarity"] else "—"
                print(f"  {r['path']}")
                print(f"    {r['current']} → {r['suggested']}  ({r['reason']}, {sim_str})")
        else:
            print("[green]All notes are already in their suggested folder.[/green]")

        print(f"\n[bold]Current distribution:[/bold]")
        for folder, count in current_dist.most_common():
            pct = 100 * count // len(rows)
            print(f"  {folder}: {count} ({pct}%)")

        if moves_needed:
            print(f"\n[bold]Projected distribution (after moves):[/bold]")
            for folder, count in suggested_dist.most_common():
                pct = 100 * count // len(rows)
                print(f"  {folder}: {count} ({pct}%)")

        if apply and moves_needed:
            if not yes:
                from rich.prompt import Confirm
                if not Confirm.ask(f"\nMove {len(moves_needed)} note(s)?", default=False):
                    print("Aborted.")
                    return
            batch = [
                {"source": r["path"], "destination": f"{r['suggested']}/{Path(r['path']).name}"}
                for r in moves_needed
            ]
            result = move_notes(batch)
            print(f"\n[green]✅ Moved {result['moved']}[/green]")
            if result.get("errors"):
                for e in result["errors"]:
                    print(f"[red]  Error:[/red] {e['source']} — {e['error']}")
        elif moves_needed and not apply:
            print(f"\n[dim]Run with --apply to move {len(moves_needed)} note(s)[/dim]")
        return

    # ── Single note ──────────────────────────────────────────────────────────
    if note is None:
        print("[red]Provide a note filename or use --all[/red]")
        raise typer.Exit(1)

    stem = Path(note).stem
    found = list(vault.rglob(f"{stem}.md"))
    if not found:
        print(f"[red]Note not found: {note}[/red]")
        raise typer.Exit(1)
    note_path = found[0]

    result = suggest_folder(vault, note_path, threshold=threshold, type_fallback=type_fb)
    if result["suggested_folder"]:
        sim_str = f"{result['similarity']:.2f}" if result["similarity"] else "—"
        print(f"\n[green]Suggested:[/green] {result['suggested_folder']}/  ({result['reason']}, {sim_str})")
        if result.get("scores"):
            top = sorted(result["scores"].items(), key=lambda kv: kv[1], reverse=True)[:5]
            print("[dim]Top scores:[/dim]")
            for folder, score in top:
                print(f"  {folder}: {score:.3f}")
        vote = result.get("neighbor_vote", {})
        if vote and vote.get("suggested_folder"):
            print(f"[dim]Neighbour vote:[/dim] {vote['suggested_folder']} ({vote.get('reason', '')})")
        if apply:
            src = str(note_path.relative_to(vault))
            dst = f"{result['suggested_folder']}/{note_path.name}"
            move_notes([{"source": src, "destination": dst}])
            print(f"[green]✅ Moved to {dst}[/green]")
    else:
        print(f"[yellow]No suggestion ({result['reason']})[/yellow]")
        if result.get("scores"):
            top = sorted(result["scores"].items(), key=lambda kv: kv[1], reverse=True)[:5]
            print("[dim]Best scores (all below threshold):[/dim]")
            for folder, score in top:
                print(f"  {folder}: {score:.3f}")


@app.command(name="describe")
def describe_cmd(
    all_: bool = typer.Option(
        False, "--all", help="Regenerate source:auto descriptions (never touches source:manual)"
    ),
    folder: str = typer.Option(
        None, "--folder", help="Operate on one folder only (vault-relative path)"
    ),
    set_terms: str = typer.Option(
        None, "--set", help="Explicitly set terms for --folder (marks source:manual)"
    ),
):
    """Generate or update per-folder description files (_folder.md)

    Generates descriptions for folders that have none. Idempotent — existing
    source:manual descriptions are never overwritten by automatic runs.
    """
    from archiver_rag.utils import get_vault_path, load_config
    from archiver_rag.vault.folder_notes import (
        describable_folders,
        read_folder_note,
        write_folder_note,
        FolderNote,
    )
    from archiver_rag.graph.terms import extract_terms_all, extract_terms
    from datetime import date

    vault = Path(get_vault_path())
    cfg = load_config()
    term_extraction_min_notes = cfg.get("advanced", {}).get("term_extraction_min_notes", 4)
    max_terms = cfg.get("advanced", {}).get("max_terms", 6)
    mmr_lambda = cfg.get("advanced", {}).get("mmr_lambda", 0.5)

    # --set requires --folder
    if set_terms is not None and folder is None:
        print("[red]--set requires --folder[/red]")
        raise typer.Exit(1)

    if set_terms is not None:
        # Explicit authorship: parse comma-separated terms, write source:manual
        terms = [t.strip() for t in set_terms.split(",") if t.strip()]
        existing = read_folder_note(vault, folder)
        note = FolderNote(
            rel_folder=folder,
            description_terms=terms,
            distinctive=existing.distinctive if existing else [],
            note_count=existing.note_count if existing else 0,
            updated=date.today().isoformat(),
            source="manual",
        )
        path = write_folder_note(vault, note)
        print(f"[green]✅ Set (manual):[/green] {folder} → {terms}")
        print(f"[dim]{path}[/dim]")
        return

    # Determine which folders to process
    if folder is not None:
        targets = [folder]
    else:
        targets = describable_folders(vault)

    if not targets:
        print("[dim]No describable folders found.[/dim]")
        return

    from archiver_rag.vault.folder_notes import apply_extracted_terms

    # Pre-extract for all folders in one pass (shared IDF table)
    print(f"[yellow]Extracting terms for {len(targets)} folder(s)...[/yellow]")
    all_terms = extract_terms_all(
        vault,
        term_extraction_min_notes=term_extraction_min_notes,
        max_terms=max_terms,
        mmr_lambda=mmr_lambda,
    )

    alpha_scale = cfg.get("advanced", {}).get("alpha_curve", {}).get("scale", 1.0)

    written = 0
    skipped = 0
    for rel_folder in targets:
        existing = read_folder_note(vault, rel_folder)
        if existing is not None and existing.source == "manual":
            print(f"[dim]  skipped (manual):[/dim] {rel_folder}")
            skipped += 1
            continue
        if existing is not None and not all_:
            print(f"[dim]  skipped (exists):[/dim] {rel_folder}")
            skipped += 1
            continue

        desc_terms_new, dist_terms_new = all_terms.get(rel_folder, ([], []))
        result = apply_extracted_terms(
            vault, rel_folder, desc_terms_new, dist_terms_new,
            alpha_scale=alpha_scale, max_terms=max_terms,
        )

        if result["gravity_well_warning"]:
            note = result["folder_note"]
            print(
                f"[yellow]  ⚠️ gravity-well forming:[/yellow] {rel_folder} "
                f"(count → {note.note_count}, α={result['alpha']:.2f})"
            )

        src_label = "auto" if result["action"] == "created" else "regenerated"
        print(
            f"[green]  {src_label}:[/green] {rel_folder} → {result['folder_note'].description_terms[:4]}"
        )
        written += 1

    print(f"\n[green]✅ {written} written, {skipped} skipped[/green]")


@app.command(name="config")
def config_cmd(
    auto_cluster: bool = typer.Option(
        None, "--auto-cluster/--no-auto-cluster", help="Enable watcher auto-clustering"
    ),
    cluster_threshold: int = typer.Option(
        None, "--cluster-threshold", help="New notes before full re-cluster"
    ),
    placement_threshold: float = typer.Option(
        None, "--placement-threshold", help="Cosine similarity threshold for semantic placement (0–1, default 0.55)"
    ),
    type_fallback: bool = typer.Option(
        None, "--type-fallback/--no-type-fallback", help="Fall back to frontmatter type: when similarity is below threshold"
    ),
    auto_describe: bool = typer.Option(
        None, "--auto-describe/--no-auto-describe", help="Enable watcher auto-regeneration of folder descriptions on membership change"
    ),
):
    """Update archiver-rag configuration"""
    import json
    from archiver_rag.init_cmd import load_config, CONFIG_PATH

    cfg = load_config()
    if auto_cluster is not None:
        cfg["auto_cluster"] = auto_cluster
    if cluster_threshold is not None:
        cfg["cluster_threshold"] = cluster_threshold
    if placement_threshold is not None:
        cfg["placement_similarity_threshold"] = placement_threshold
    if type_fallback is not None:
        cfg["type_fallback"] = type_fallback
    if auto_describe is not None:
        cfg["auto_describe"] = auto_describe
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
