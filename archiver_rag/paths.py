"""Single source of truth for archiver-rag's on-disk paths (XDG Base Directories).

Every function here is deliberately plain — never a module-level constant baked at
import time — so a test can monkeypatch e.g. `paths.config_dir` directly. Consumers
must import this module qualified (`from archiver_rag import paths`, then
`paths.config_path()`), never `from archiver_rag.paths import config_path` — that
way one monkeypatch of `archiver_rag.paths.config_path` reaches every call site,
without the per-module rebinding conftest.py has to do for get_vault_path.

macOS gets the Unix backend forced, not platformdirs' native ~/Library/... default:
this machine's other CLIs (nvim, git, gh, zed, starship) all already use ~/.config,
so that's the idiomatic location for a tool in this world, not just on Linux.
Windows is roadmap-only (see CLAUDE.md) but left on platformdirs' normal
auto-detected backend so it isn't actively broken if someone tries it.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from archiver_rag.utils import log

APP_NAME = "archiver-rag"


def _dirs():
    if sys.platform == "win32":
        from platformdirs import PlatformDirs

        return PlatformDirs(APP_NAME, appauthor=False)
    from platformdirs.unix import Unix

    return Unix(appname=APP_NAME, appauthor=False)


def config_dir() -> Path:
    return Path(_dirs().user_config_dir)


def data_dir() -> Path:
    return Path(_dirs().user_data_dir)


def cache_dir() -> Path:
    return Path(_dirs().user_cache_dir)


def config_path() -> Path:
    return config_dir() / "config.json"


def default_chroma_path() -> Path:
    return data_dir() / "chroma_db"


def centroids_path() -> Path:
    return data_dir() / "centroids.json"


def legacy_dir() -> Path:
    """Pre-XDG install location. Read during migration only — never written to."""
    return Path.home() / ".archiver-rag"


_migration_checked = False


def ensure_migrated() -> None:
    """Move a legacy ~/.archiver-rag install to the new XDG paths, once per process.

    Cheap no-op on every call after the first (or when there's nothing to migrate).
    """
    global _migration_checked
    if _migration_checked:
        return
    _migration_checked = True
    _migrate_legacy_install()


def _migrate_legacy_install() -> None:
    legacy = legacy_dir()
    new_config = config_path()
    if new_config.exists() or not legacy.exists():
        return

    config_dir().mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)

    moved: list[str] = []

    legacy_config = legacy / "config.json"
    if legacy_config.exists():
        shutil.move(str(legacy_config), str(new_config))
        moved.append(f"config.json -> {new_config}")
        try:
            cfg = json.loads(new_config.read_text())
            cfg["chroma_path"] = str(default_chroma_path())
            cfg["install_path"] = str(data_dir())
            new_config.write_text(json.dumps(cfg, indent=2))
        except Exception:
            pass

    legacy_chroma = legacy / "chroma_db"
    new_chroma = default_chroma_path()
    if legacy_chroma.exists() and not new_chroma.exists():
        shutil.move(str(legacy_chroma), str(new_chroma))
        moved.append(f"chroma_db/ -> {new_chroma}")

    legacy_centroids = legacy / "centroids.json"
    new_centroids = centroids_path()
    if legacy_centroids.exists() and not new_centroids.exists():
        shutil.move(str(legacy_centroids), str(new_centroids))
        moved.append(f"centroids.json -> {new_centroids}")

    if moved:
        log("Migrated legacy ~/.archiver-rag install to XDG paths:")
        for line in moved:
            log(f"  {line}")
        log(f"Old directory left at {legacy} -- safe to remove manually once verified.")
