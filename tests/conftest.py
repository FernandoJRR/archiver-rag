"""
Pytest fixtures for archiver-rag tests.

_no_real_vault (autouse): blocks accidental hits to the real vault by raising on
get_vault_path. Each module binds its own reference via "from archiver_rag.utils import
get_vault_path", so we patch every module's binding, not just utils.

tmp_vault (opt-in): repoints those same bindings at tmp_path/vault and returns a helper
that creates notes by relative path.
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
