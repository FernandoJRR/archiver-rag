"""
Tests for delete_notes() in vault/notes.py.

Uses the tmp_vault fixture (disk-backed); no ChromaDB or embedder involved.
prune_orphans is patched out to avoid real DB calls.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_prune(monkeypatch):
    """Block real ChromaDB calls. delete_notes imports prune_orphans lazily inside the
    function body, so patching the source module is enough — the name resolves at call time."""
    monkeypatch.setattr(
        "archiver_rag.core.ingest.prune_orphans",
        lambda *_a, **_kw: 0,
        raising=False,
    )


def _delete(tmp_vault, notes: list[str]):
    from archiver_rag.vault.notes import delete_notes
    return delete_notes(notes)


# ── basic move to .trash/ ─────────────────────────────────────────────────────

def test_note_lands_in_trash(tmp_vault):
    tmp_vault.write("alpha.md", "# Alpha\n\nContent.")
    result = _delete(tmp_vault, ["alpha.md"])
    assert result["deleted"] == ["alpha.md"]
    assert result["errors"] == []
    trash = tmp_vault.root / ".trash" / "alpha.md"
    assert trash.exists()
    assert not (tmp_vault.root / "alpha.md").exists()


def test_trash_collision_generates_suffix(tmp_vault):
    tmp_vault.write("alpha.md", "# Alpha first")
    tmp_vault.write("sub/alpha.md", "# Alpha second")
    # Pre-occupy .trash/alpha.md
    trash_dir = tmp_vault.root / ".trash"
    trash_dir.mkdir(exist_ok=True)
    (trash_dir / "alpha.md").write_text("pre-existing", encoding="utf-8")

    result = _delete(tmp_vault, ["sub/alpha.md"])
    assert result["deleted"] == ["sub/alpha.md"]
    # Collision resolved
    assert (trash_dir / "alpha-1.md").exists()


# ── inbound link sweep ────────────────────────────────────────────────────────

def test_inbound_link_swept(tmp_vault):
    tmp_vault.write("target.md", "# Target\n\nContent.")
    tmp_vault.write("linker.md", "# Linker\n\nBody.\n\n## Related\n- [[target]]")

    result = _delete(tmp_vault, ["target.md"])
    assert result["deleted"] == ["target.md"]
    linker_content = (tmp_vault.root / "linker.md").read_text()
    assert "[[target]]" not in linker_content


def test_no_inbound_links_no_sweep(tmp_vault):
    tmp_vault.write("isolated.md", "# Isolated\n\nNo links to me.")
    result = _delete(tmp_vault, ["isolated.md"])
    assert result["deleted"] == ["isolated.md"]
    assert result["links_cleaned"] == []


# ── path traversal protection ─────────────────────────────────────────────────

def test_path_traversal_rejected(tmp_vault):
    result = _delete(tmp_vault, ["../escape.md"])
    assert result["deleted"] == []
    assert result["errors"][0]["error"] == "Path outside vault boundary"


# ── .trash/ is only created when something actually moves ────────────────────

def test_failed_delete_leaves_no_trash_dir(tmp_vault):
    """A call that deletes nothing must not mutate the vault."""
    result = _delete(tmp_vault, ["../escape.md", "missing.md"])
    assert result["deleted"] == []
    assert not (tmp_vault.root / ".trash").exists(), "empty .trash/ created by a no-op delete"


def test_successful_delete_creates_trash_dir(tmp_vault):
    tmp_vault.write("real.md", "# Real")
    _delete(tmp_vault, ["real.md"])
    assert (tmp_vault.root / ".trash" / "real.md").exists()


# ── file not found ────────────────────────────────────────────────────────────

def test_missing_note_error(tmp_vault):
    result = _delete(tmp_vault, ["nonexistent.md"])
    assert result["deleted"] == []
    assert result["errors"][0]["source"] == "nonexistent.md"


def test_missing_does_not_block_others(tmp_vault):
    tmp_vault.write("real.md", "# Real")
    result = _delete(tmp_vault, ["nonexistent.md", "real.md"])
    assert "real.md" in result["deleted"]
    assert any(e["source"] == "nonexistent.md" for e in result["errors"])


# ── multiple notes — single sweep pass ───────────────────────────────────────

def test_multiple_notes_single_sweep(tmp_vault):
    tmp_vault.write("a.md", "# A")
    tmp_vault.write("b.md", "# B")
    tmp_vault.write("linker.md", "# L\n\n## Related\n- [[a]]\n- [[b]]")
    result = _delete(tmp_vault, ["a.md", "b.md"])
    assert set(result["deleted"]) == {"a.md", "b.md"}
    linker_content = (tmp_vault.root / "linker.md").read_text()
    assert "[[a]]" not in linker_content
    assert "[[b]]" not in linker_content


# ── .trash notes not counted as valid stems ───────────────────────────────────

def test_trash_not_valid_stem(tmp_vault):
    """After deletion, the trashed note's stem must not appear in valid_stems."""
    tmp_vault.write("gone.md", "# Gone")
    tmp_vault.write("other.md", "# Other\n\n## Related\n- [[gone]]")
    _delete(tmp_vault, ["gone.md"])
    content = (tmp_vault.root / "other.md").read_text()
    assert "[[gone]]" not in content
