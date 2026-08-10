"""Verify _folder.md is excluded from every note-enumeration site."""

import pytest
from pathlib import Path
from archiver_rag.utils import (
    FOLDER_NOTE_NAME,
    note_stems,
    build_link_map,
    is_indexable_note,
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers — _no_real_vault is autouse, so no tmp_vault fixture needed here;
# we pass vault paths explicitly to each function under test.
# ──────────────────────────────────────────────────────────────────────────────

def make_populated_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()

    # A real note
    decision = vault / "decision"
    decision.mkdir()
    (decision / "my-note.md").write_text("# Note\n[[other-note]]\n", encoding="utf-8")

    # The sidecar that must be invisible
    (decision / FOLDER_NOTE_NAME).write_text(
        "---\ndescription_terms: [clustering]\nsource: auto\n---\n",
        encoding="utf-8",
    )
    return vault


# ──────────────────────────────────────────────────────────────────────────────
# is_indexable_note
# ──────────────────────────────────────────────────────────────────────────────

def test_folder_note_not_indexable(tmp_path):
    p = tmp_path / "decision" / FOLDER_NOTE_NAME
    p.parent.mkdir(parents=True)
    p.touch()
    assert not is_indexable_note(p)


def test_real_note_is_indexable(tmp_path):
    p = tmp_path / "decision" / "my-note.md"
    p.parent.mkdir(parents=True)
    p.touch()
    assert is_indexable_note(p)


def test_hidden_note_not_indexable(tmp_path):
    p = tmp_path / ".trash" / "my-note.md"
    p.parent.mkdir(parents=True)
    p.touch()
    assert not is_indexable_note(p)


# ──────────────────────────────────────────────────────────────────────────────
# note_stems
# ──────────────────────────────────────────────────────────────────────────────

def test_note_stems_excludes_folder_note(tmp_path):
    vault = make_populated_vault(tmp_path)
    stems = note_stems(vault)
    assert FOLDER_NOTE_NAME.replace(".md", "") not in stems
    assert "_folder" not in stems
    assert "my-note" in stems


# ──────────────────────────────────────────────────────────────────────────────
# build_link_map
# ──────────────────────────────────────────────────────────────────────────────

def test_build_link_map_excludes_folder_note(tmp_path):
    vault = make_populated_vault(tmp_path)
    # Give the folder note a wikilink — it must not appear as a linker
    (vault / "decision" / FOLDER_NOTE_NAME).write_text(
        "---\nsource: auto\n---\n[[my-note]]\n", encoding="utf-8"
    )
    outgoing, incoming = build_link_map(vault)
    assert "_folder" not in outgoing
    # The wikilink written by _folder.md must not appear in incoming either
    assert "my-note" not in incoming or "_folder" not in incoming.get("my-note", [])


# ──────────────────────────────────────────────────────────────────────────────
# clustering._build_adjacency (uses rglob)
# ──────────────────────────────────────────────────────────────────────────────

def test_adjacency_excludes_folder_note(tmp_path, monkeypatch):
    vault = make_populated_vault(tmp_path)

    from archiver_rag.graph import clustering

    monkeypatch.setattr(clustering, "get_vault_path", lambda: str(vault))
    adj = clustering._build_adjacency(vault)

    assert "_folder" not in adj


# ──────────────────────────────────────────────────────────────────────────────
# vault_status (health.py) excludes _folder.md from total_notes
# ──────────────────────────────────────────────────────────────────────────────

def test_vault_status_excludes_folder_note(tmp_path, monkeypatch):
    vault = make_populated_vault(tmp_path)

    from archiver_rag.vault import health

    monkeypatch.setattr(health, "get_vault_path", lambda: str(vault))
    status = health.vault_status()

    assert status["structure"]["total_notes"] == 1  # my-note.md only
