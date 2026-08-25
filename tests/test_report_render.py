"""Rendering for `archiver-rag status` / `archiver-rag health`.

These are the commands you reach for when something already looks wrong, so the
property under test is that they always produce a diagnosis: an unconfigured install, a
dead ChromaDB, a missing heartbeat, and a crash-looping service must each render a line
that says so, rather than raising or — worse — printing a reassuring "✅ Running".

Rendering takes the composed dict as its only argument, which is what keeps the human
output and `--json` from ever disagreeing.
"""

from __future__ import annotations

import json

from archiver_rag import report


def _status(**overrides) -> dict:
    base = {
        "service": {
            "platform": "darwin", "installed": True, "loaded": True, "running": True,
            "pid": 17249, "last_exit_status": 0, "service_file": "/x.plist",
            "stdout_log": "/tmp/archiver-rag.log", "stderr_log": "/tmp/e.log",
        },
        "runtime": {
            "pid": 17249, "started_at": "2026-08-24T09:00:00",
            "vault_path": "/v", "last_event": "2026-08-24T09:30:00",
            "last_event_kind": "modified", "last_event_path": "decision/a.md",
            "counts": {"ingested": 42, "placed": 3, "described": 5, "swept": 1, "deleted": 0},
        },
        "config": {
            "configured": True, "vault_path": "/v", "auto_cluster": True,
            "auto_describe": True, "placement_similarity_threshold": 0.5,
            "type_fallback": True,
            "paths": {
                "config_file": "/c/config.json", "config_exists": True,
                "data_dir": "/d", "cache_dir": "/ca",
            },
        },
        "vault": {"total_notes": 78, "total_folders": 14},
        "index": {
            "chunks": 812, "indexed_notes": 78, "notes_on_disk": 78,
            "missing_from_index": [], "orphaned_in_index": [], "stale": [],
            "counts": {"missing_from_index": 0, "orphaned_in_index": 0, "stale": 0},
            "newest_mtime": "2026-08-24T09:30:00", "error": None,
        },
        "placement": {
            "describable": 14, "competing": 9, "competing_folders": ["Projects/X"],
            "undescribed": ["a", "b", "c", "d", "e"], "manual_locked": ["decision"],
            "centroids_cached": 9, "error": None,
        },
    }
    base.update(overrides)
    return base


def test_healthy_status_renders_running_and_in_sync(capsys):
    report.render_status(_status())
    out = capsys.readouterr().out

    assert "Running" in out and "17249" in out
    assert "index matches disk" in out
    assert "42 ingested" in out
    assert "9 of 14 folders competing" in out


def test_crash_loop_is_not_reported_as_running(capsys):
    """launchctl exits 0 for a loaded-but-dead job — the bug the old status() had."""
    svc = _status()["service"] | {"running": False, "pid": None, "last_exit_status": 1}
    report.render_status(_status(service=svc))
    out = capsys.readouterr().out

    assert "Loaded but not running" in out
    assert "last exit 1" in out
    assert "Running —" not in out


def test_not_installed_points_at_init(capsys):
    svc = _status()["service"] | {"installed": False, "loaded": False, "running": False}
    report.render_status(_status(service=svc))

    assert "archiver-rag init" in capsys.readouterr().out


def test_missing_heartbeat_says_restart_not_dead(capsys):
    """A watcher predating this feature is alive but silent — don't imply it crashed."""
    report.render_status(_status(runtime={}))
    out = capsys.readouterr().out

    assert "no heartbeat" in out
    assert "restart" in out


def test_pid_mismatch_surfaces_a_silent_restart(capsys):
    rt = _status()["runtime"] | {"pid": 999}
    report.render_status(_status(runtime=rt))

    assert "watcher restarted" in capsys.readouterr().out


def test_unconfigured_status_renders_instead_of_raising(capsys):
    cfg = _status()["config"] | {"configured": False, "vault_path": None}
    report.render_status(_status(config=cfg, vault=None, index=None, placement=None))
    out = capsys.readouterr().out

    assert "Not configured" in out
    assert "archiver-rag init" in out


def test_drift_names_the_repair_command(capsys):
    idx = _status()["index"] | {
        "counts": {"missing_from_index": 6, "orphaned_in_index": 2, "stale": 0}
    }
    report.render_status(_status(index=idx))
    out = capsys.readouterr().out

    assert "6 not indexed" in out and "2 orphaned" in out
    assert "archiver-rag sync" in out


def test_orphans_only_recommends_prune(capsys):
    idx = _status()["index"] | {
        "counts": {"missing_from_index": 0, "orphaned_in_index": 2, "stale": 0}
    }
    report.render_status(_status(index=idx))

    assert "archiver-rag prune" in capsys.readouterr().out


def test_unreachable_index_renders_the_error(capsys):
    idx = _status()["index"] | {"error": "FileNotFoundError: not configured"}
    report.render_status(_status(index=idx))

    assert "FileNotFoundError" in capsys.readouterr().out


def test_status_report_is_json_serializable():
    json.loads(json.dumps(_status(), default=str))


# ── health ────────────────────────────────────────────────────────────────────

def _health(**overrides) -> dict:
    base = {
        "configured": True,
        "vault_path": "/v",
        "index": _status()["index"],
        "vault_error": None,
        "vault": {
            "structure": {"total_notes": 78, "total_folders": 14, "folders": []},
            "health": {
                "orphaned_notes": [], "no_frontmatter": [], "empty_notes": [],
                "broken_links": [],
                "counts": {
                    "orphaned_notes": 0, "no_frontmatter": 0,
                    "empty_notes": 0, "broken_links": 0,
                },
            },
            "tags": {"most_used": [("architecture", 22)], "total_unique": 31},
            "recent": {"modified": ["a.md"], "created": ["b.md"]},
        },
    }
    base.update(overrides)
    return base


def test_clean_health_still_prints_every_check(capsys):
    """A silent section reads as an omission — 'checked, fine' must be visible."""
    report.render_health(_health())
    out = capsys.readouterr().out

    assert "frontmatter: all 78 notes" in out
    assert "no empty notes" in out
    assert "no broken wikilinks" in out
    assert "architecture 22" in out


def test_health_lists_problems_with_their_fix(capsys):
    vault = _health()["vault"]
    vault["health"]["broken_links"] = ["decision/bar.md → [[baz]]"]
    vault["health"]["counts"]["broken_links"] = 1
    idx = _health()["index"] | {
        "missing_from_index": ["decision/new.md"],
        "counts": {"missing_from_index": 1, "orphaned_in_index": 0, "stale": 0},
    }
    report.render_health(_health(vault=vault, index=idx))
    out = capsys.readouterr().out

    assert "broken wikilinks: 1" in out
    assert "decision/bar.md" in out
    assert "archiver-rag sync" in out


def test_health_truncation_says_how_many_are_hidden(capsys):
    vault = _health()["vault"]
    vault["health"]["orphaned_notes"] = [f"n{i}.md" for i in range(20)]
    vault["health"]["counts"]["orphaned_notes"] = 37  # real total exceeds the capped list
    report.render_health(_health(vault=vault))

    assert "and 27 more" in capsys.readouterr().out


def test_unconfigured_health_renders_instead_of_raising(capsys):
    report.render_health({"configured": False, "vault_path": None, "index": None, "vault": None})

    assert "Not configured" in capsys.readouterr().out


def test_health_survives_a_vault_scan_failure(capsys):
    report.render_health(_health(vault=None, vault_error="OSError: boom"))
    out = capsys.readouterr().out

    assert "OSError: boom" in out
    assert "812 chunks" in out  # index side still reported


def test_compose_health_reports_unconfigured_rather_than_exiting():
    """utils.load_config ({}), never init_cmd.load_config (typer.Exit)."""
    assert report.compose_health()["configured"] is False


def test_compose_status_reports_unconfigured_rather_than_exiting():
    composed = report.compose_status()
    assert composed["config"]["configured"] is False
    assert composed["index"] is None
    json.loads(json.dumps(composed, default=str))
