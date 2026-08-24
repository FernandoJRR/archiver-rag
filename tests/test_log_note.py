"""Tests for vault/notes.py::log_note() — Gate 1 folder-birth description.

log_note() creates vault/{type}/ via mkdir with no sidecar. Gate 1 adds: if the
target folder currently has no _folder.md, extract terms from the note that just
landed there (tag-based, since n=1 note) and write a real description instead of
leaving the folder orphaned until auto_describe (if even on) eventually catches up.
"""

from __future__ import annotations

from archiver_rag.vault.folder_notes import FolderNote, read_folder_note, write_folder_note
from archiver_rag.vault.notes import log_note


def test_new_folder_gets_real_description(tmp_vault):
    log_note(
        title="First note",
        content="Some content about watchers and clustering.",
        type="gotcha",
        tags=["watcher", "clustering"],
    )

    note = read_folder_note(tmp_vault.root, "gotcha")
    assert note is not None
    assert note.description_terms, "must be a real description, not an empty placeholder"
    assert note.source == "auto"


def test_existing_manual_description_is_untouched(tmp_vault):
    write_folder_note(
        tmp_vault.root,
        FolderNote(rel_folder="reference", description_terms=["api", "docs"], source="manual"),
    )

    log_note(title="New note", content="Body.", type="reference", tags=["unrelated"])

    note = read_folder_note(tmp_vault.root, "reference")
    assert note.description_terms == ["api", "docs"]
    assert note.source == "manual"


def test_existing_auto_description_is_not_immediately_regenerated(tmp_vault):
    """log_note only describes an undescribed folder — ongoing freshness for an
    already-described folder stays auto_describe's job (separately gated, watcher-driven),
    not something every log_note call should pay the cost of."""
    write_folder_note(
        tmp_vault.root,
        FolderNote(rel_folder="decision", description_terms=["old-term"], note_count=1, source="auto"),
    )

    log_note(title="New note", content="Body.", type="decision", tags=["new-term"])

    note = read_folder_note(tmp_vault.root, "decision")
    assert note.description_terms == ["old-term"]


def test_term_extraction_failure_does_not_break_note_creation(tmp_vault, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("embedding service unavailable")

    monkeypatch.setattr("archiver_rag.graph.terms.extract_terms", _boom)

    result = log_note(title="Resilient note", content="Body.", type="lesson")

    assert result["created"] == "lesson/resilient-note.md"
    assert (tmp_vault.root / "lesson" / "resilient-note.md").exists()
