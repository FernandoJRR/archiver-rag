"""
Pytest fixtures for archiver-rag tests.

_no_real_vault (autouse): blocks accidental hits to the real vault by raising on
get_vault_path. Each module binds its own reference via "from archiver_rag.utils import
get_vault_path", so we patch every module's binding, not just utils.

tmp_vault (opt-in): repoints those same bindings at tmp_path/vault and returns a helper
that creates notes by relative path.

_no_real_home_paths (autouse): blocks accidental hits to the real ~/.config/archiver-rag,
~/.local/share/archiver-rag, ~/.cache/archiver-rag, and ~/.archiver-rag by redirecting
archiver_rag.paths' getters to tmp_path and forcing the migration check to a no-op. Every
consumer calls through archiver_rag.paths (module-qualified), so patching that module once
is enough — no per-module rebinding needed the way get_vault_path requires.

tmp_install (opt-in): resets the migration guard and exposes the same tmp_path locations
for tests that specifically exercise archiver_rag.paths.ensure_migrated().
"""

from __future__ import annotations

import pytest
from pathlib import Path


_MODULES_WITH_VAULT = [
    "archiver_rag.vault.health",
    "archiver_rag.vault.notes",
    "archiver_rag.vault.reorganize",
    "archiver_rag.graph.clustering",
    "archiver_rag.graph.linker",
    "archiver_rag.core.ingest",
    "archiver_rag.watcher",
]


@pytest.fixture(autouse=True)
def _no_real_vault(monkeypatch):
    def _raise():
        raise RuntimeError(
            "get_vault_path() called in a test — use the tmp_vault fixture"
        )

    import archiver_rag.utils as _utils

    monkeypatch.setattr(_utils, "get_vault_path", _raise)

    for mod_name in _MODULES_WITH_VAULT:
        try:
            import importlib

            mod = importlib.import_module(mod_name)
            if hasattr(mod, "get_vault_path"):
                monkeypatch.setattr(mod, "get_vault_path", _raise)
        except ImportError:
            pass


@pytest.fixture(autouse=True)
def _no_real_home_paths(tmp_path, monkeypatch):
    import archiver_rag.paths as _paths

    fake_config = tmp_path / "config-dir"
    fake_data = tmp_path / "data-dir"
    fake_cache = tmp_path / "cache-dir"
    fake_legacy = tmp_path / "legacy-dir-does-not-exist"

    monkeypatch.setattr(_paths, "config_dir", lambda: fake_config)
    monkeypatch.setattr(_paths, "data_dir", lambda: fake_data)
    monkeypatch.setattr(_paths, "cache_dir", lambda: fake_cache)
    monkeypatch.setattr(_paths, "legacy_dir", lambda: fake_legacy)
    monkeypatch.setattr(_paths, "_migration_checked", True)


@pytest.fixture
def tmp_install(tmp_path, monkeypatch):
    import archiver_rag.paths as _paths

    fake_config = tmp_path / "config-dir"
    fake_data = tmp_path / "data-dir"
    fake_cache = tmp_path / "cache-dir"
    fake_legacy = tmp_path / "legacy-dir"

    monkeypatch.setattr(_paths, "config_dir", lambda: fake_config)
    monkeypatch.setattr(_paths, "data_dir", lambda: fake_data)
    monkeypatch.setattr(_paths, "cache_dir", lambda: fake_cache)
    monkeypatch.setattr(_paths, "legacy_dir", lambda: fake_legacy)
    monkeypatch.setattr(_paths, "_migration_checked", False)

    return {
        "config_dir": fake_config,
        "data_dir": fake_data,
        "cache_dir": fake_cache,
        "legacy_dir": fake_legacy,
    }


@pytest.fixture
def anyio_backend():
    """anyio's pytest plugin runs @pytest.mark.anyio tests on every backend it can find.

    Pinning asyncio keeps the HTTP transport tests from also being run under trio, which
    is not a supported runtime for this project and is not installed.
    """
    return "asyncio"


class _VaultBuilder:
    def __init__(self, root: Path):
        self.root = root

    def write(self, rel_path: str, text: str) -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()

    def _get_vault_path() -> str:
        return str(vault)

    import archiver_rag.utils as _utils

    monkeypatch.setattr(_utils, "get_vault_path", _get_vault_path)

    for mod_name in _MODULES_WITH_VAULT:
        try:
            import importlib

            mod = importlib.import_module(mod_name)
            if hasattr(mod, "get_vault_path"):
                monkeypatch.setattr(mod, "get_vault_path", _get_vault_path)
        except ImportError:
            pass

    return _VaultBuilder(vault)
