"""Tests for vault/folder_notes.py — FolderNote read/write/discovery."""

import pytest
from pathlib import Path
from archiver_rag.vault.folder_notes import (
    FolderNote,
    read_folder_note,
    write_folder_note,
    described_folders,
    describable_folders,
)
from archiver_rag.utils import FOLDER_NOTE_NAME


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def write_note(vault: Path, rel: str, text: str = "# Note\nContent.") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ──────────────────────────────────────────────────────────────────────────────
# read_folder_note
# ──────────────────────────────────────────────────────────────────────────────

def test_read_absent_returns_none(tmp_path):
    vault = make_vault(tmp_path)
    assert read_folder_note(vault, "decision") is None


def test_roundtrip(tmp_path):
    vault = make_vault(tmp_path)
    note = FolderNote(
        rel_folder="decision",
        description_terms=["clustering", "watcher"],
        distinctive=["label-propagation"],
        note_count=12,
        updated="2026-08-09",
        source="auto",
    )
    write_folder_note(vault, note)
    back = read_folder_note(vault, "decision")
    assert back is not None
    assert back.description_terms == ["clustering", "watcher"]
    assert back.distinctive == ["label-propagation"]
    assert back.note_count == 12
    assert back.source == "auto"


def test_malformed_yaml_returns_none(tmp_path):
    vault = make_vault(tmp_path)
    folder = vault / "gotcha"
    folder.mkdir()
    (folder / FOLDER_NOTE_NAME).write_text("not yaml at all }{", encoding="utf-8")
    assert read_folder_note(vault, "gotcha") is None


def test_missing_frontmatter_returns_none(tmp_path):
    vault = make_vault(tmp_path)
    folder = vault / "gotcha"
    folder.mkdir()
    (folder / FOLDER_NOTE_NAME).write_text("just plain text\n", encoding="utf-8")
    assert read_folder_note(vault, "gotcha") is None


def test_source_manual_preserved(tmp_path):
    vault = make_vault(tmp_path)
    note = FolderNote(
        rel_folder="reference",
        description_terms=["api", "docs"],
        source="manual",
    )
    write_folder_note(vault, note)
    back = read_folder_note(vault, "reference")
    assert back is not None
    assert back.source == "manual"


# ──────────────────────────────────────────────────────────────────────────────
# described_folders
# ──────────────────────────────────────────────────────────────────────────────

def test_described_folders_empty(tmp_path):
    vault = make_vault(tmp_path)
    assert described_folders(vault) == {}


def test_described_folders_found(tmp_path):
    vault = make_vault(tmp_path)
    note = FolderNote(rel_folder="decision", description_terms=["clustering"])
    write_folder_note(vault, note)
    result = described_folders(vault)
    assert "decision" in result
    assert result["decision"].description_terms == ["clustering"]


def test_described_folders_ignores_hidden(tmp_path):
    vault = make_vault(tmp_path)
    hidden = vault / ".obsidian"
    hidden.mkdir()
    (hidden / FOLDER_NOTE_NAME).write_text(
        "---\ndescription_terms: [x]\nsource: auto\n---\n", encoding="utf-8"
    )
    assert described_folders(vault) == {}


# ──────────────────────────────────────────────────────────────────────────────
# describable_folders
# ──────────────────────────────────────────────────────────────────────────────

def test_describable_folders_excludes_root(tmp_path):
    vault = make_vault(tmp_path)
    write_note(vault, "root-note.md")
    # root notes should not make the root itself appear as a describable folder
    result = describable_folders(vault)
    assert "." not in result
    assert "" not in result


def test_describable_folders_excludes_empty_dirs(tmp_path):
    vault = make_vault(tmp_path)
    (vault / "empty-dir").mkdir()
    assert describable_folders(vault) == []


def test_describable_folders_excludes_hidden(tmp_path):
    vault = make_vault(tmp_path)
    write_note(vault, ".trash/secret.md")
    assert describable_folders(vault) == []


def test_describable_folders_excludes_folder_note_itself(tmp_path):
    """A folder whose only .md file is _folder.md should not be describable."""
    vault = make_vault(tmp_path)
    folder = vault / "decision"
    folder.mkdir()
    (folder / FOLDER_NOTE_NAME).write_text("---\nsource: auto\n---\n", encoding="utf-8")
    assert describable_folders(vault) == []


def test_describable_folders_counts_direct_only(tmp_path):
    """Parent with no direct notes is not describable even if a subfolder has notes."""
    vault = make_vault(tmp_path)
    write_note(vault, "Projects/sub/note.md")
    result = describable_folders(vault)
    assert "Projects" not in result
    assert "Projects/sub" in result


def test_describable_folders_includes_correct(tmp_path):
    vault = make_vault(tmp_path)
    write_note(vault, "decision/foo.md")
    write_note(vault, "reference/bar.md")
    result = describable_folders(vault)
    assert "decision" in result
    assert "reference" in result
