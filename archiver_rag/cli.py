import typer
from pathlib import Path
from rich import print

app = typer.Typer(name="archiver-rag", help="Semantic RAG for Obsidian vaults + MCP")


@app.callback()
def _main():
    from archiver_rag import paths

    paths.ensure_migrated()


@app.command()
def init():
    """Interactive setup wizard — run this first"""
    from archiver_rag.init_cmd import run_init

    run_init()


@app.command()
def start(
    target: str = typer.Argument("watcher", help="watcher or http"),
    login: bool = typer.Option(
        None, "--login/--no-login",
        help="HTTP only: start automatically at login (asks when omitted)",
    ),
    stateful: bool = typer.Option(
        False, "--stateful", help="HTTP only: sessions + SSE instead of stateless JSON"
    ),
    allowed_host: list[str] = typer.Option(
        [], "--allowed-host",
        help="HTTP only: DNS-rebinding allowlist entry (repeatable)",
    ),
    host: str = typer.Option(
        None, "--host", help="HTTP only: bind address override (baked into the service)"
    ),
    port: int = typer.Option(
        None, "--port", help="HTTP only: port override (baked into the service)"
    ),
    path: str = typer.Option(
        None, "--path", help="HTTP only: route override (baked into the service)"
    ),
):
    """Start the vault watcher, or the detached MCP HTTP server"""
    from archiver_rag import service

    defn = _resolve_target(target)
    if defn is service.WATCHER:
        service.start(service.WATCHER)
        return
    _start_http(
        login=login, stateful=stateful, allowed_hosts=list(allowed_host),
        host=host, port=port, path=path,
    )


def _resolve_target(target: str):
    from archiver_rag import service

    if target == "watcher":
        return service.WATCHER
    if target == "http":
        return service.HTTP
    print(f"[red]Unknown service: {target}[/red] — use 'watcher' or 'http'")
    raise typer.Exit(1)


def _start_http(
    *, login, stateful: bool, allowed_hosts: list[str],
    host=None, port=None, path=None,
) -> None:
    from archiver_rag import service

    st = service.http_state()
    # daemon_endpoint() honors flags baked into the installed service file, so an
    # already-running overridden daemon is described by its real address.
    url, cfg_host, cfg_port, cfg_path = service.daemon_endpoint()
    # Explicit overrides win over everything — they are baked into the service file,
    # so the daemon's actual bind address is what gets printed here.
    host = host or cfg_host
    port = port or cfg_port
    path = path or cfg_path
    url = f"http://{host}:{port}{path}"

    if st.get("running"):
        print(f"[green]✅ MCP HTTP server already running[/green] — {url}")
        print(f"  [dim]PID {st['pid']} · restart with [bold]archiver-rag restart http[/bold][/dim]")
        return

    # A busy port means someone else owns it (a foreground serve, most likely).
    # Loading anyway would KeepAlive-crash-loop on the bind failure.
    if service.port_in_use(host, port):
        print(f"[red]❌ {host}:{port} is already in use by another process.[/red]")
        print("  [dim]Stop that process first (foreground serve?), or pick another --port[/dim]")
        raise typer.Exit(1)

    if login is None:
        login = typer.confirm("Start automatically at login?", default=False)

    args = ["serve", "--transport", "http"]
    if host != cfg_host:
        args += ["--host", host]
    if port != cfg_port:
        args += ["--port", str(port)]
    if path != cfg_path:
        args += ["--path", path]
    if stateful:
        args.append("--stateful")
    for h in allowed_hosts:
        args += ["--allowed-host", h]

    service.write_service(service.HTTP, args, run_at_load=login)
    service.start(service.HTTP)

    print(f"[green]MCP over HTTP:[/green] {url}")
    if not _is_loopback(host):
        print(
            f"\n[yellow]⚠️  Binding to {host} with NO authentication.[/yellow]\n"
            "    Every tool is exposed: the full vault is readable, and\n"
            "    log_note / move_notes / cluster_vault can modify it.\n"
            "    Put a TLS-terminating reverse proxy, VPN, or SSH tunnel in front.\n"
        )
    print("[dim]Add the URL to your agents' MCP registry yourself "
          "(e.g. claude mcp add --scope user --transport http archiver-rag "
          f"{url})[/dim]")


@app.command()
def stop(
    target: str = typer.Argument("watcher", help="watcher or http"),
):
    """Stop the vault watcher, or the detached MCP HTTP server"""
    from archiver_rag import service

    service.stop(_resolve_target(target))


@app.command()
def restart(
    target: str = typer.Argument("watcher", help="watcher or http"),
):
    """Restart the vault watcher, or the detached MCP HTTP server"""
    from archiver_rag import service

    defn = _resolve_target(target)
    if not service.state(defn).get("installed"):
        print(f"[red]❌ {target} is not installed[/red] — run [bold]archiver-rag start {target}[/bold]")
        raise typer.Exit(1)
    service.stop(defn)
    service.start(defn)


@app.command()
def status(
    json_out: bool = typer.Option(
        False, "--json", help="Emit the raw report as JSON"
    ),
):
    """Service liveness, watcher activity, index drift, and effective config"""
    import builtins
    import json as _json
    from archiver_rag.report import compose_status, render_status

    report = compose_status()
    if json_out:
        # builtins.print, not rich's — rich would interpret [..] in paths as markup
        # and hard-wrap long lines, neither of which survives a pipe into jq.
        builtins.print(_json.dumps(report, indent=2, default=str))
        return
    render_status(report)


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
def health(
    json_out: bool = typer.Option(
        False, "--json", help="Emit the raw report as JSON"
    ),
):
    """Index-vs-disk drift plus vault health — orphans, broken links, tags"""
    import builtins
    import json as _json
    from archiver_rag.report import compose_health, render_health

    report = compose_health()
    if json_out:
        builtins.print(_json.dumps(report, indent=2, default=str))
        return
    render_health(report)


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

    # 1. Stop and remove both services (watcher + detached HTTP server)
    from archiver_rag import service

    for defn in (service.WATCHER, service.HTTP):
        if sys.platform == "darwin":
            if defn.plist_path.exists():
                subprocess.run(["launchctl", "unload", str(defn.plist_path)], capture_output=True)
                defn.plist_path.unlink()
                print(f"[yellow]Service removed: {defn.label}[/yellow]")

        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", defn.unit_name],
                capture_output=True,
            )
            if defn.unit_path.exists():
                defn.unit_path.unlink()
                print(f"[yellow]Service removed: {defn.label}[/yellow]")

    # 2. Remove MCP from ~/.claude.json
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        config = json.loads(claude_json.read_text())
        config.get("mcpServers", {}).pop("archiver-rag", None)
        claude_json.write_text(json.dumps(config, indent=2))
        print("[yellow]🔌 MCP entry removed from Claude Code[/yellow]")

    # 3. Remove config/data/cache directories (and any pre-XDG leftovers)
    import shutil
    from archiver_rag import paths

    for d in (paths.config_dir(), paths.data_dir(), paths.cache_dir(), paths.legacy_dir()):
        if d.exists():
            shutil.rmtree(d)
            print(f"[yellow]🗑  Removed {d}[/yellow]")

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
    w_identity = float(cfg.get("advanced", {}).get("placement_weights", {}).get("identity", 0.6))
    w_content = float(cfg.get("advanced", {}).get("placement_weights", {}).get("content", 0.4))
    name_prefix_bonus = float(cfg.get("advanced", {}).get("name_prefix_bonus", 0.15))

    if all_notes:
        # ── §9.7 vault-wide dry-run / batch move ────────────────────────────
        from archiver_rag.utils import is_indexable_note

        all_note_paths = [f for f in vault.rglob("*.md") if is_indexable_note(f)]
        if not all_note_paths:
            print("[dim]No notes found.[/dim]")
            return

        rows = []
        for np_ in sorted(all_note_paths):
            suggestion = suggest_folder(vault, np_, threshold=threshold, type_fallback=type_fb, w_identity=w_identity, w_content=w_content, name_prefix_bonus=name_prefix_bonus)
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

    result = suggest_folder(vault, note_path, threshold=threshold, type_fallback=type_fb, w_identity=w_identity, w_content=w_content, name_prefix_bonus=name_prefix_bonus)
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
    tag_terms_in_description = cfg.get("advanced", {}).get("tag_terms_in_description", True)

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
        tag_terms_in_description=tag_terms_in_description,
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


@app.command()
def relink(
    apply: bool = typer.Option(
        False, "--apply", help="Rewrite ## Related sections (default: dry-run report only)"
    ),
    margin: float = typer.Option(
        None, "--margin", help="Override advanced.link_margin for this run (default: config, 0.05)"
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation on --apply"),
    restart_watcher: bool = typer.Option(
        True,
        "--restart-watcher/--no-restart-watcher",
        help="If the watcher is running, stop it for the rewrite and start it back up after (default: on)",
    ),
    sync_after: bool = typer.Option(
        True,
        "--sync/--no-sync",
        help="Re-embed rewritten notes afterward via sync_vault (default: on)",
    ),
):
    """One-time repair: rebuild every note's ## Related section under the margin rule.

    auto_link's old per-run append-only cap let ## Related links accumulate without
    bound (5 new per save, nothing ever removed a still-resolving link) — this vault
    measured mean 25.2 links/note, density 0.66, before the margin rule replaced it.
    The margin rule only stops *future* growth; it doesn't retroactively trim what's
    already there. This command applies the same rebuild, in one pass, to every note.

    Dry-run by default: prints per-note before/after Related-link counts and
    vault-wide density, writes nothing. Use --apply to actually rewrite.

    If the watcher is running when --apply writes, it is stopped first and restarted
    once the writes finish (see --no-restart-watcher to manage this yourself instead)
    — each rewrite would otherwise fire a watcher re-ingest + auto_link mid-pass, and
    worse, interleave with this command's own in-progress candidate-selection queries
    against the same ChromaDB collection. Dry-run analysis never writes, so it's safe
    to run alongside a live watcher regardless of this flag.

    After writing, `sync_vault` runs by default (see --no-sync) — mtime-based, so it
    re-embeds only the notes this command actually rewrote rather than the whole
    vault (`archiver-rag index` would re-embed every note unconditionally, wasteful
    when relink typically touches a subset). Runs inside the same stopped-watcher
    window as the rewrite, before the watcher restarts, so nothing observes a stale
    index in between.
    """
    from archiver_rag.utils import get_vault_path, is_indexable_note, note_stems
    from archiver_rag.graph.linker import (
        _append_links_section,
        _find_related_section,
        _get_existing_links,
        _get_link_margin_config,
        select_related_candidates,
    )
    from archiver_rag.wikilinks import iter_wikilinks
    from archiver_rag import service

    vault = Path(get_vault_path())
    notes = sorted(f for f in vault.rglob("*.md") if is_indexable_note(f))
    if not notes:
        print("[dim]No notes found.[/dim]")
        return

    cfg_margin, max_total_links = _get_link_margin_config()
    link_margin = margin if margin is not None else cfg_margin
    valid = note_stems(vault)

    def _related_count(content: str) -> int:
        loc = _find_related_section(content)
        if loc is None:
            return 0
        return len(list(iter_wikilinks(content[loc[1] : loc[2]], skip_code=False)))

    rows: list[tuple[str, int, int]] = []
    total_before = 0
    total_after = 0
    pending_writes: list[tuple[Path, str]] = []

    with typer.progressbar(notes, label="Analyzing notes") as progress:
        for note in progress:
            content = note.read_text(encoding="utf-8", errors="ignore")
            if not content.strip():
                continue
            before = _related_count(content)

            existing_links = _get_existing_links(content)
            top_links, keep_targets = select_related_candidates(
                content, note.stem, existing_links, 0.55, link_margin, max_total_links
            )
            updated = _append_links_section(content, top_links, valid, keep_targets)
            after = _related_count(updated) if updated is not content else before

            total_before += before
            total_after += after
            if after != before:
                rel = str(note.relative_to(vault))
                rows.append((rel, before, after))
                if updated is not content:
                    pending_writes.append((note, updated))

    n = len(notes)
    denom = n * (n - 1) if n > 1 else 1
    density_before = round(total_before / denom, 3)
    density_after = round(total_after / denom, 3)

    print(f"\n[bold]Relink report — {len(notes)} notes, margin={link_margin}[/bold]\n")
    if rows:
        for rel, before, after in sorted(rows, key=lambda r: r[1] - r[2], reverse=True)[:30]:
            print(f"  {rel}: {before} → {after}")
        if len(rows) > 30:
            print(f"  [dim]… and {len(rows) - 30} more[/dim]")
    else:
        print("[green]No notes would change.[/green]")

    print(f"\n[bold]Related-links total:[/bold] {total_before} → {total_after}")
    print(f"[bold]Density estimate:[/bold] {density_before} → {density_after}")

    if not apply:
        print(f"\n[dim]Dry-run — run with --apply to rewrite {len(pending_writes)} note(s)[/dim]")
        return

    if not pending_writes:
        print("\n[green]Nothing to write.[/green]")
        return

    watcher_was_running = restart_watcher and service.service_state().get("running", False)

    if not yes:
        from rich.prompt import Confirm

        prompt = f"\nRewrite {len(pending_writes)} note(s)?"
        if watcher_was_running:
            prompt += " (watcher will be stopped during the rewrite"
            prompt += ", re-embedded, then restarted)" if sync_after else ", then restarted)"
        elif service.service_state().get("running", False):
            # restart_watcher=False and the watcher is live — writes will race it.
            prompt += " [yellow](watcher is running and will NOT be stopped — writes may race it)[/yellow]"
        if not Confirm.ask(prompt, default=False):
            print("Aborted.")
            return

    if watcher_was_running:
        print("[dim]Stopping watcher for the rewrite...[/dim]")
        service.stop()

    try:
        for note, updated in pending_writes:
            note.write_text(updated, encoding="utf-8")
        print(f"\n[green]✅ Rewrote {len(pending_writes)} note(s)[/green]")

        if sync_after:
            from archiver_rag.core.ingest import sync_vault

            print("[dim]Re-embedding rewritten notes (sync)...[/dim]")
            sync_result = sync_vault(str(vault))
            print(
                f"[green]✅ Synced:[/green] {sync_result['indexed']} re-embedded, "
                f"{sync_result['up_to_date']} already current, {sync_result['pruned']} pruned"
            )
        else:
            print("[dim]Run `archiver-rag sync` next so embedded chunks reflect the repaired Related sections.[/dim]")
    finally:
        if watcher_was_running:
            print("[dim]Restarting watcher...[/dim]")
            service.start()


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
    from archiver_rag import paths
    from archiver_rag.init_cmd import load_config

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
    paths.config_path().write_text(json.dumps(cfg, indent=2))
    print("[green]✅ Config updated[/green]")


@app.command(name="serve", hidden=True)
def serve(
    transport: str = typer.Option(
        "stdio", "--transport", help="stdio (default) or http"
    ),
    host: str = typer.Option(None, "--host", help="HTTP bind address"),
    port: int = typer.Option(None, "--port", help="HTTP port"),
    path: str = typer.Option(None, "--path", help="HTTP route, e.g. /mcp"),
    allowed_host: list[str] = typer.Option(
        [], "--allowed-host",
        help="Enable DNS-rebinding protection for this Host (repeatable)",
    ),
    stateful: bool = typer.Option(
        False, "--stateful", help="Use HTTP sessions + SSE instead of stateless JSON"
    ),
):
    """Internal — runs the MCP server (stdio by default, or streamable HTTP)"""
    import asyncio

    if transport == "stdio":
        from archiver_rag.mcp.server import main

        asyncio.run(main())
        return

    if transport != "http":
        print(f"[red]Unknown transport: {transport}[/red] — use 'stdio' or 'http'")
        raise typer.Exit(1)

    from archiver_rag.mcp import http as mcp_http

    # configured_endpoint() shares serve's exact resolution ({} on error → loopback
    # defaults), so the detached server and everything that describes it agree.
    # Explicit flags still win over config.
    _, cfg_host, cfg_port, cfg_path = mcp_http.configured_endpoint()
    host = host or cfg_host
    port = port or cfg_port
    path = path or cfg_path

    if not _is_loopback(host):
        # Not a prompt — this must never block a service start — but it must be
        # impossible to miss. There is no auth layer in archiver-rag by design.
        print(
            f"\n[yellow]⚠️  Binding to {host} with NO authentication.[/yellow]\n"
            "    Every tool is exposed: the full vault is readable, and\n"
            "    log_note / move_notes / cluster_vault can modify it.\n"
            "    Put a TLS-terminating reverse proxy, VPN, or SSH tunnel in front.\n"
        )

    print(f"[green]MCP over HTTP:[/green] http://{host}:{port}{path}")
    mcp_http.serve_http(
        host=host, port=port, path=path,
        stateless=not stateful,
        allowed_hosts=list(allowed_host),
    )


def _is_loopback(host: str) -> bool:
    import ipaddress

    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we cannot classify without resolving it — warn rather than assume
        # it is safe.
        return False


@app.command(name="watch", hidden=True)
def watch(vault_path: str = typer.Argument(...)):
    """Internal — runs the watcher (called by the service)"""
    from archiver_rag.watcher import watch

    watch(vault_path)


if __name__ == "__main__":
    app()
