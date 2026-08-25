"""Composition and rendering for `archiver-rag status` and `archiver-rag health`.

Kept out of cli.py so both commands stay the thin wrappers every other command in this
CLI is, and so the composed dicts can be rendered in tests without a Typer runner.

The seam is deliberate: each command composes one plain dict, and rendering takes that
dict as its only argument. `--json` prints the same dict, so the human output and the
machine output cannot drift apart or disagree about what "in sync" means.

Neither command may abort. These are the commands you run *because* something looks
wrong, so an unconfigured install, a dead Chroma, or a missing heartbeat must each
render as a diagnosis rather than a traceback.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich import print


# ──────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _ago(ts) -> str:
    dt = _parse(ts)
    if dt is None:
        return "—"
    return f"{_duration((datetime.now() - dt).total_seconds())} ago"


def _mark(ok: bool) -> str:
    return "[green]✅[/green]" if ok else "[yellow]⚠️[/yellow]"


def _onoff(value) -> str:
    return "[green]on[/green]" if value else "[dim]off[/dim]"


def _sample(items: list, limit: int = 5) -> str:
    shown = ", ".join(str(i) for i in items[:limit])
    extra = len(items) - limit
    return f"{shown} …(+{extra} more)" if extra > 0 else shown


# ──────────────────────────────────────────────────────────────────────────────
# status
# ──────────────────────────────────────────────────────────────────────────────

def compose_status() -> dict:
    from archiver_rag import runtime
    from archiver_rag.core.index_stats import index_stats
    from archiver_rag.service import service_state
    from archiver_rag.utils import load_config

    # utils.load_config (returns {}) — deliberately not init_cmd.load_config, which
    # raises typer.Exit(1). "Not configured" is a status report, not a failure.
    config = load_config()
    vault_path = config.get("vault_path")

    report: dict = {
        "service": service_state(),
        "runtime": runtime.read_state(),
        "config": {
            "configured": bool(vault_path),
            "vault_path": vault_path,
            "auto_cluster": config.get("auto_cluster"),
            "auto_describe": config.get("auto_describe"),
            "placement_similarity_threshold": config.get(
                "placement_similarity_threshold"
            ),
            "type_fallback": config.get("type_fallback"),
            "paths": _config_paths(),
        },
        "vault": None,
        "index": None,
        "placement": None,
    }
    if not vault_path or not Path(vault_path).is_dir():
        return report

    vault = Path(vault_path)
    report["index"] = index_stats(vault)
    report["vault"] = {
        "total_notes": report["index"]["notes_on_disk"],
        "total_folders": sum(
            1
            for d in vault.rglob("*")
            if d.is_dir() and not any(part.startswith(".") for part in d.parts)
        ),
    }
    report["placement"] = _placement_state(vault)
    return report


def _config_paths() -> dict:
    from archiver_rag import paths

    config_file = paths.config_path()
    return {
        "config_file": str(config_file),
        "config_exists": config_file.exists(),
        "data_dir": str(paths.data_dir()),
        "cache_dir": str(paths.cache_dir()),
    }


def _placement_state(vault: Path) -> dict:
    """Which folders can actually win a placement right now.

    Reads _folder.md frontmatter and the centroid cache only — cached_centroids() rather
    than folder_centroids(), because the latter re-embeds every stale folder as a side
    effect of being called, and a status command must be cheap and must not mutate.
    """
    try:
        from archiver_rag.graph.centroids import cached_centroids
        from archiver_rag.vault.folder_notes import (
            describable_folders,
            described_folders,
            read_folder_note,
        )

        describable = set(describable_folders(vault))
        described = described_folders(vault)
        # A described folder whose terms are empty has description_text() == "", which
        # folder_centroids() skips — it is declared but not competing. That is exactly
        # how the type-folders (decision/, lesson/, …) are deliberately locked out.
        competing = {k for k, v in described.items() if v.description_terms}
        manual_locked = sorted(
            f
            for f in describable
            if (n := read_folder_note(vault, f)) is not None
            and n.source == "manual"
            and not n.description_terms
        )
        return {
            "describable": len(describable),
            "competing": len(competing),
            "competing_folders": sorted(competing),
            "undescribed": sorted(describable - set(described)),
            "manual_locked": manual_locked,
            "centroids_cached": len(cached_centroids()),
            "error": None,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def render_status(report: dict) -> None:
    svc = report["service"]
    rt = report["runtime"]
    cfg = report["config"]

    print("\n[bold]Service[/bold]")
    if svc.get("running"):
        pid = svc.get("pid")
        started = rt.get("started_at")
        dt = _parse(started)
        uptime = (
            f", up {_duration((datetime.now() - dt).total_seconds())}" if dt else ""
        )
        print(f"  [green]✅ Running[/green] — PID {pid}{uptime}")
    elif svc.get("loaded"):
        # launchctl exits 0 for a loaded-but-dead job, so this is the crash-loop case
        # the old one-line status() reported as "✅ Running".
        exit_status = svc.get("last_exit_status")
        print(
            f"  [yellow]⚠️ Loaded but not running[/yellow] "
            f"(last exit {exit_status if exit_status is not None else '—'})"
        )
        print("  [dim]KeepAlive may be restart-looping — check the error log[/dim]")
    elif not svc.get("installed"):
        print("  [red]❌ Not installed[/red] — run [bold]archiver-rag init[/bold]")
    else:
        print("  [red]❌ Not running[/red] — run [bold]archiver-rag start[/bold]")
    if svc.get("error"):
        print(f"  [dim]{svc['error']}[/dim]")
    if svc.get("stdout_log"):
        print(f"  [dim]logs: {svc['stdout_log']}[/dim]")

    print("\n[bold]Activity[/bold]")
    if not rt:
        print(
            "  [dim]no heartbeat — restart the watcher "
            "([bold]archiver-rag restart[/bold]) to enable activity tracking[/dim]"
        )
    else:
        if rt.get("last_event"):
            kind = rt.get("last_event_kind") or "event"
            where = rt.get("last_event_path") or ""
            print(f"  last event: {_ago(rt['last_event'])} — {kind} {where}")
        else:
            print("  last event: [dim]— (nothing since start)[/dim]")
        counts = rt.get("counts") or {}
        summary = " · ".join(f"{v} {k}" for k, v in counts.items()) or "—"
        print(f"  since start: {summary}")
        # launchd's KeepAlive restarts the watcher silently; a PID that no longer
        # matches the one that wrote the heartbeat is the only visible trace.
        if svc.get("running") and rt.get("pid") and svc.get("pid") != rt.get("pid"):
            print(
                f"  [yellow]⚠️ watcher restarted "
                f"(PID {rt['pid']} → {svc['pid']})[/yellow]"
            )

    print("\n[bold]Vault[/bold]")
    if not cfg["configured"]:
        print("  [red]❌ Not configured[/red] — run [bold]archiver-rag init[/bold]")
    elif report["vault"] is None:
        print(f"  [red]❌ Vault path does not exist: {cfg['vault_path']}[/red]")
    else:
        v, idx = report["vault"], report["index"]
        print(f"  {cfg['vault_path']}")
        print(f"  {v['total_notes']} notes · {v['total_folders']} folders")
        _render_index_line(idx)

    if report["placement"] is not None:
        pl = report["placement"]
        print("\n[bold]Placement[/bold]")
        if pl.get("error"):
            print(f"  [yellow]⚠️ unavailable: {pl['error']}[/yellow]")
        else:
            print(
                f"  auto_cluster: {_onoff(cfg['auto_cluster'])}   "
                f"threshold: {cfg['placement_similarity_threshold'] or '—'}   "
                f"type_fallback: {_onoff(cfg['type_fallback'])}"
            )
            print(f"  auto_describe: {_onoff(cfg['auto_describe'])}")
            print(
                f"  {pl['competing']} of {pl['describable']} folders competing "
                f"({len(pl['undescribed'])} undescribed · "
                f"{len(pl['manual_locked'])} manual-locked)"
            )
            if pl["undescribed"]:
                print(f"    [dim]undescribed: {_sample(pl['undescribed'])}[/dim]")
            print(f"  centroid cache: {pl['centroids_cached']} entries")

    p = cfg["paths"]
    print("\n[bold]Config[/bold]")
    print(f"  {_mark(p['config_exists'])} {p['config_file']}")
    print(f"  [dim]data: {p['data_dir']}[/dim]")
    print(f"  [dim]cache: {p['cache_dir']}[/dim]")
    print()


def _render_index_line(idx: dict) -> None:
    """Counts plus verdict — for `status`, where the index is one line among many."""
    if idx.get("error"):
        print(f"  index: [red]❌ unavailable — {idx['error']}[/red]")
        return
    print(f"  index: {idx['chunks']} chunks across {idx['indexed_notes']} notes")
    _render_index_verdict(idx)


def _render_index_verdict(idx: dict) -> None:
    """Just the in-sync/drift verdict — health has already printed the counts."""
    from archiver_rag.core.index_stats import in_sync

    counts = idx["counts"]
    if in_sync(idx):
        print("  [green]✅ index matches disk[/green]")
        return
    problems = []
    if counts["missing_from_index"]:
        problems.append(f"{counts['missing_from_index']} not indexed")
    if counts["orphaned_in_index"]:
        problems.append(f"{counts['orphaned_in_index']} orphaned")
    if counts["stale"]:
        problems.append(f"{counts['stale']} stale")
    fix = (
        "archiver-rag prune"
        if counts["orphaned_in_index"] and not counts["missing_from_index"]
        else "archiver-rag sync"
    )
    print(
        f"  [yellow]⚠️ {' · '.join(problems)}[/yellow] — run [bold]{fix}[/bold]"
    )


# ──────────────────────────────────────────────────────────────────────────────
# health
# ──────────────────────────────────────────────────────────────────────────────

def compose_health() -> dict:
    from archiver_rag.core.index_stats import index_stats
    from archiver_rag.utils import load_config

    config = load_config()
    vault_path = config.get("vault_path")
    if not vault_path or not Path(vault_path).is_dir():
        return {"configured": False, "vault_path": vault_path, "index": None, "vault": None}

    vault = Path(vault_path)
    report: dict = {
        "configured": True,
        "vault_path": vault_path,
        "index": index_stats(vault),
        "vault": None,
        "vault_error": None,
    }
    try:
        from archiver_rag.vault.health import vault_status

        report["vault"] = vault_status()
    except Exception as e:
        report["vault_error"] = f"{type(e).__name__}: {e}"
    return report


def render_health(report: dict) -> None:
    if not report.get("configured"):
        print("\n[red]❌ Not configured[/red] — run [bold]archiver-rag init[/bold]\n")
        return

    idx = report["index"]
    print("\n[bold]Index[/bold]")
    if idx.get("error"):
        print(f"  [red]❌ unavailable — {idx['error']}[/red]")
    else:
        newest = f" · newest {_ago(idx['newest_mtime'])}" if idx["newest_mtime"] else ""
        print(
            f"  {idx['chunks']} chunks · {idx['indexed_notes']} notes indexed · "
            f"{idx['notes_on_disk']} on disk{newest}"
        )
        _render_index_verdict(idx)
        for key, label, fix in (
            ("missing_from_index", "not indexed", "archiver-rag sync"),
            ("orphaned_in_index", "indexed but gone from disk", "archiver-rag prune"),
            ("stale", "changed since last index", "archiver-rag sync"),
        ):
            if idx["counts"][key]:
                print(
                    f"  [yellow]⚠️ {idx['counts'][key]} {label}[/yellow] "
                    f"[dim](fix: {fix})[/dim]"
                )
                for item in idx[key][:10]:
                    print(f"      {item}")

    if report.get("vault_error"):
        print(f"\n[bold]Vault health[/bold]\n  [red]❌ {report['vault_error']}[/red]\n")
        return

    vs = report["vault"]
    health = vs["health"]
    counts = health["counts"]
    total = vs["structure"]["total_notes"]

    print("\n[bold]Vault health[/bold]")
    # Clean checks still print. "Everything I looked at is fine" is the answer this
    # command most often needs to give, and a silent section reads as an omission.
    _health_line(
        counts["no_frontmatter"], f"frontmatter: all {total} notes",
        "missing frontmatter", health["no_frontmatter"],
    )
    _health_line(
        counts["empty_notes"], "no empty notes", "empty notes", health["empty_notes"]
    )
    _health_line(
        counts["broken_links"], "no broken wikilinks", "broken wikilinks",
        health["broken_links"],
    )
    _health_line(
        counts["orphaned_notes"], "every note has inbound links",
        "orphaned (no inbound links)", health["orphaned_notes"],
    )

    print("\n[bold]Tags[/bold]")
    print(f"  {vs['tags']['total_unique']} unique across {total} notes")
    if vs["tags"]["most_used"]:
        print(
            "  " + " · ".join(f"{t} {c}" for t, c in vs["tags"]["most_used"][:8])
        )

    print("\n[bold]Recent[/bold]")
    for label, key in (("modified", "modified"), ("created", "created")):
        items = vs["recent"][key]
        print(f"  {label}:")
        # One per line: note slugs routinely run past 70 chars, and a comma-joined
        # list of five of them wraps into an unreadable block.
        for item in items or []:
            print(f"      {item}")
        if not items:
            print("      —")
    print()


def _health_line(count: int, ok_label: str, problem_label: str, items: list) -> None:
    if not count:
        print(f"  [green]✅[/green] {ok_label}")
        return
    print(f"  [yellow]⚠️[/yellow] {problem_label}: {count}")
    for item in items[:10]:
        print(f"      {item}")
    if count > len(items[:10]):
        print(f"      [dim]…and {count - len(items[:10])} more[/dim]")
