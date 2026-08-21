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


def apply_extracted_terms(
    vault: Path,
    rel_folder: str,
    desc_terms_new: list[str],
    dist_terms_new: list[str],
    *,
    alpha_scale: float = 1.0,
    max_terms: int = 6,
) -> dict:
    """Decide/blend/write a folder's description from freshly extracted terms.

    Shared by `describe_cmd` (CLI, terms pre-extracted in bulk via extract_terms_all)
    and the watcher's `_maybe_redescribe` (terms extracted per-folder via extract_terms) —
    both need the identical skip-manual / blend-via-alpha / pathology-check / write
    policy, only the extraction strategy differs.

    Returns:
        {
          "action": "created" | "regenerated" | "skipped_manual",
          "folder_note": FolderNote | None,   # None only when skipped
          "alpha": float | None,              # None when created fresh or skipped
          "gravity_well_warning": bool,
        }
    """
    from datetime import date
    from archiver_rag.graph.terms import alpha_for, blend_terms

    existing = read_folder_note(vault, rel_folder)
    if existing is not None and existing.source == "manual":
        return {
            "action": "skipped_manual",
            "folder_note": None,
            "alpha": None,
            "gravity_well_warning": False,
        }

    folder_dir = vault / rel_folder
    direct_notes = (
        [f for f in folder_dir.iterdir() if f.is_file() and is_indexable_note(f)]
        if folder_dir.exists()
        else []
    )

    gravity_well_warning = False
    alpha: float | None = None
    if existing is not None and existing.description_terms:
        alpha = alpha_for(existing.note_count, scale=alpha_scale)
        desc_terms = blend_terms(existing.description_terms, desc_terms_new, alpha, max_terms)
        if len(direct_notes) > existing.note_count and existing.note_count > 0:
            growth_ratio = len(direct_notes) / existing.note_count
            if growth_ratio >= 1.15 and alpha < 0.4:
                gravity_well_warning = True
    else:
        desc_terms = desc_terms_new

    note = FolderNote(
        rel_folder=rel_folder,
        description_terms=desc_terms,
        distinctive=dist_terms_new,
        note_count=len(direct_notes),
        updated=date.today().isoformat(),
        source="auto",
    )
    write_folder_note(vault, note)

    return {
        "action": "created" if existing is None else "regenerated",
        "folder_note": note,
        "alpha": alpha,
        "gravity_well_warning": gravity_well_warning,
    }


def described_folders(vault: Path) -> dict[str, FolderNote]:
    """Folders with a readable _folder.md AND at least one real note on disk right now,
    keyed by vault-relative path.

    A folder that emptied out (all its notes moved or deleted elsewhere) keeps its stale
    _folder.md on disk — nothing deletes that sidecar automatically — so without this
    check it would keep competing as a placement candidate forever. This is the fix for
    the folder-collapse incident: emptied note-stem-named folders (e.g.
    archiver-rag-sync-command/, left behind at 0 notes after cluster_vault redistributed
    its members elsewhere) stayed in folder_centroids() as live candidates indefinitely.
    Counts real files on disk, not the frontmatter `note_count` field — that field goes
    stale exactly when a folder empties out, which is the case this guards against.
    described_folders() has exactly one production caller (graph/centroids.py::
    folder_centroids()), so this filter reaches every placement/clustering candidate list
    without needing a second fix at the call site.
    """
    result: dict[str, FolderNote] = {}
    for fn in vault.rglob(FOLDER_NOTE_NAME):
        if any(p.startswith(".") for p in fn.parts):
            continue
        try:
            rel_folder = str(fn.parent.relative_to(vault))
        except ValueError:
            continue
        folder_dir = fn.parent
        has_notes = any(
            f.is_file() and is_indexable_note(f) for f in folder_dir.iterdir()
        )
        if not has_notes:
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
