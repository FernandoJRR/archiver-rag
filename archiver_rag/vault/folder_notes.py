"""Read/write the per-folder description sidecar (_folder.md).

Every public function receives `vault: Path` explicitly and never calls
get_vault_path() at module level, so this module stays out of
conftest._MODULES_WITH_VAULT and is safe in tests without the tmp_vault fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from archiver_rag.utils import FOLDER_NOTE_NAME, is_indexable_note


@dataclass
class FolderNote:
    rel_folder: str  # vault-relative, e.g. "decision" or "Projects/WeeklyCuisine"
    description_terms: list[str] = field(default_factory=list)
    distinctive: list[str] = field(default_factory=list)
    note_count: int = 0
    updated: str = ""
    source: str = "auto"  # "auto" | "manual"
    # §4.1 meters — computed but weighted 0.0 until activated by config
    term_dispersion: float = 0.0
    embedding_compactness: float = 0.0


def _folder_note_path(vault: Path, rel_folder: str) -> Path:
    return vault / rel_folder / FOLDER_NOTE_NAME


def read_folder_note(vault: Path, rel_folder: str) -> FolderNote | None:
    """Return the parsed FolderNote, or None if absent or malformed."""
    path = _folder_note_path(vault, rel_folder)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        end = content.index("---", 3)
        fm = yaml.safe_load(content[3:end].strip()) or {}
        return FolderNote(
            rel_folder=rel_folder,
            description_terms=list(fm.get("description_terms") or []),
            distinctive=list(fm.get("distinctive") or []),
            note_count=int(fm.get("note_count") or 0),
            updated=str(fm.get("updated") or ""),
            source=str(fm.get("source") or "auto"),
            term_dispersion=float(fm.get("term_dispersion") or 0.0),
            embedding_compactness=float(fm.get("embedding_compactness") or 0.0),
        )
    except Exception:
        return None


def write_folder_note(vault: Path, note: FolderNote) -> Path:
    """Write (or overwrite) the sidecar. Creates the folder directory if needed."""
    folder_dir = vault / note.rel_folder
    folder_dir.mkdir(parents=True, exist_ok=True)

    terms_str = yaml.dump(note.description_terms, default_flow_style=True).strip()
    dist_str = yaml.dump(note.distinctive, default_flow_style=True).strip()

    fm_lines = [
        "---",
        f"description_terms: {terms_str}",
        f"distinctive: {dist_str}",
        f"note_count: {note.note_count}",
        f"updated: {note.updated or date.today().isoformat()}",
        f"source: {note.source}",
        f"term_dispersion: {round(note.term_dispersion, 4)}",
        f"embedding_compactness: {round(note.embedding_compactness, 4)}",
        "---",
    ]

    # Human-readable template body (slot substitution, no LLM)
    terms_preview = ", ".join(note.description_terms[:3])
    dist_preview = ", ".join(note.distinctive[:2])
    if terms_preview and dist_preview:
        body = f"Themes {terms_preview}. Terms: {dist_preview}."
    elif terms_preview:
        body = f"Themes {terms_preview}."
    else:
        body = ""

    content = "\n".join(fm_lines)
    if body:
        content += f"\n{body}\n"

    path = _folder_note_path(vault, note.rel_folder)
    path.write_text(content, encoding="utf-8")
    return path


def described_folders(vault: Path) -> dict[str, FolderNote]:
    """All folders that have a readable _folder.md, keyed by vault-relative path."""
    result: dict[str, FolderNote] = {}
    for fn in vault.rglob(FOLDER_NOTE_NAME):
        if any(p.startswith(".") for p in fn.parts):
            continue
        try:
            rel_folder = str(fn.parent.relative_to(vault))
        except ValueError:
            continue
        note = read_folder_note(vault, rel_folder)
        if note is not None:
            result[rel_folder] = note
    return result


def describable_folders(vault: Path) -> list[str]:
    """Vault-relative paths of non-root, non-hidden folders with ≥1 direct indexable note.

    Counts direct children only (non-recursive), because recursive counting would make
    parent and child descriptions converge on the same notes, defeating per-folder identity.
    """
    folders: list[str] = []
    for d in vault.rglob("*"):
        if not d.is_dir():
            continue
        if any(p.startswith(".") for p in d.parts):
            continue
        if d == vault:
            continue
        direct_notes = [
            f for f in d.iterdir() if f.is_file() and is_indexable_note(f)
        ]
        if direct_notes:
            folders.append(str(d.relative_to(vault)))
    return sorted(folders)
