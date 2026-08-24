import re
import json
import shutil
from datetime import date
from pathlib import Path
from archiver_rag.utils import get_vault_path, build_link_map, note_stems


# Generous enough that real titles are never clipped, far under the 255-byte
# filesystem limit. A truncated slug makes the filename disagree with the note's
# identity, which is what wikilinks are written against.
SLUG_MAX = 120


def _slugify(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^\w\s-]", "", title)
    title = re.sub(r"[\s_]+", "-", title)
    return title[:SLUG_MAX]


def _build_frontmatter(type: str, tags: list[str], related_notes: list[str]) -> str:
    lines = ["---", f"type: {type}", f"date: {date.today().isoformat()}"]
    if tags:
        lines.append(f"tags: {json.dumps(tags)}")
    if related_notes:
        lines.append("related:")
        for note in related_notes:
            # Store bare name in YAML (no [[brackets]]) — body ## Related carries the wikilinks
            name = re.sub(r"^\[\[|\]\]$", "", note)
            lines.append(f"  - {name}")
    lines.append("---")
    return "\n".join(lines)


def _resolve_filepath(vault: Path, type: str, title: str) -> Path:
    folder = vault / type
    folder.mkdir(parents=True, exist_ok=True)
    # No date prefix: the filename is the note's identity, and wikilinks are
    # written against it. The date lives in frontmatter, where it stays queryable.
    base = _slugify(title)
    filepath = folder / f"{base}.md"
    counter = 1
    while filepath.exists():
        filepath = folder / f"{base}-{counter}.md"
        counter += 1
    return filepath


def log_note(
    title: str,
    content: str,
    type: str = "note",
    tags: list[str] | None = None,
    related_notes: list[str] | None = None,
) -> dict:
    if not title.strip():
        raise ValueError("title cannot be empty")

    tags = [t for t in (tags or []) if t.strip()]
    related_notes = related_notes or []

    # Prevent path traversal: use only the final path component
    type = Path(type).name or "note"

    vault = Path(get_vault_path())
    if not vault.exists():
        raise FileNotFoundError(f"Vault not found: {vault}")

    frontmatter = _build_frontmatter(type, tags, related_notes)
    filepath = _resolve_filepath(vault, type, title)

    body_parts = [frontmatter, "", f"# {title}", "", content.strip()]
    if related_notes:
        body_parts += ["", "## Related"]
        for note in related_notes:
            wrapped = note if note.startswith("[[") else f"[[{note}]]"
            body_parts.append(f"- {wrapped}")

    filepath.write_text("\n".join(body_parts), encoding="utf-8")

    # Gate 1 — folder birth: give an undescribed folder a real description from this
    # note rather than leaving it orphaned until auto_describe (if even on) catches up.
    # Only fires when currently undescribed, not on every log_note into an existing
    # folder — an already-described large folder would otherwise pay the heavier
    # c-TF-IDF+MMR cost on every single call. Never let this break note creation.
    try:
        from archiver_rag.vault.folder_notes import apply_extracted_terms, read_folder_note

        if read_folder_note(vault, type) is None:
            from archiver_rag.graph.terms import extract_terms

            desc, dist = extract_terms(vault, type)
            apply_extracted_terms(vault, type, desc, dist)
    except Exception:
        pass

    return {
        "created": str(filepath.relative_to(vault)),
        "type": type,
        "title": title,
        "tags": tags,
        "related": related_notes,
        "path": str(filepath),
    }


def sweep_dead_links(vault: Path, stems: list[str]) -> dict:
    """Prune dead wikilink targets from ## Related in every note that links to any stem in `stems`.

    Called AFTER the target notes have already moved out of the vault (to .trash/ or elsewhere),
    so note_stems(vault) correctly excludes them and _append_links_section prunes their entries.
    """
    from archiver_rag.graph.linker import _append_links_section

    _, incoming = build_link_map(vault)
    valid = note_stems(vault)

    # Collect unique linker paths (a note may link to multiple deleted stems)
    linker_paths: set[Path] = set()
    for stem in stems:
        for linker_stem in incoming.get(stem, []):
            found = list(vault.rglob(f"{linker_stem}.md"))
            for f in found:
                if not any(p.startswith(".") for p in f.relative_to(vault).parts):
                    linker_paths.add(f)

    swept: list[str] = []
    errors: list[dict] = []

    for linker in linker_paths:
        try:
            content = linker.read_text(encoding="utf-8", errors="ignore")
            updated = _append_links_section(content, [], valid)
            if updated is not content:
                linker.write_text(updated, encoding="utf-8")
                swept.append(str(linker.relative_to(vault)))
        except Exception as e:
            errors.append({"file": str(linker.relative_to(vault)), "error": str(e)})

    return {"swept": swept, "errors": errors}


def delete_notes(notes: list[str]) -> dict:
    """Move notes to vault/.trash/ and sweep inbound wikilinks.

    `notes` are paths relative to the vault root (e.g. 'decision/foo.md').
    Returns {"deleted": [...], "links_cleaned": [...], "errors": [...]}.
    """
    vault = Path(get_vault_path())
    trash_dir = vault / ".trash"

    deleted: list[str] = []
    errors: list[dict] = []
    deleted_stems: list[str] = []

    for note_rel in notes:
        src = vault / note_rel

        # Security — prevent path traversal
        try:
            src.resolve().relative_to(vault.resolve())
        except ValueError:
            errors.append({"source": note_rel, "error": "Path outside vault boundary"})
            continue

        if not src.exists():
            errors.append({"source": note_rel, "error": "File not found"})
            continue

        # Created only once something is actually going to move, so a call that
        # deletes nothing leaves no stray directory behind.
        trash_dir.mkdir(exist_ok=True)

        # Collision-safe flat name in .trash/ (Obsidian convention)
        trash_dest = trash_dir / src.name
        counter = 1
        while trash_dest.exists():
            trash_dest = trash_dir / f"{src.stem}-{counter}{src.suffix}"
            counter += 1

        try:
            shutil.move(str(src), str(trash_dest))
            deleted.append(note_rel)
            deleted_stems.append(src.stem)
        except Exception as e:
            errors.append({"source": note_rel, "error": str(e)})

    # Sweep inbound links in a single pass after all moves (valid_stems excludes .trash)
    links_cleaned: list[str] = []
    if deleted_stems:
        sweep_result = sweep_dead_links(vault, deleted_stems)
        links_cleaned = sweep_result["swept"]
        errors.extend(sweep_result["errors"])

        # Remove orphaned chunks from ChromaDB
        from archiver_rag.core.ingest import prune_orphans

        prune_orphans(str(vault))

    return {"deleted": deleted, "links_cleaned": links_cleaned, "errors": errors}
