import re
import json
from datetime import date
from pathlib import Path
from archiver_rag.utils import get_vault_path


def _slugify(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r'[^\w\s-]', '', title)
    title = re.sub(r'[\s_]+', '-', title)
    return title[:60]


def _build_frontmatter(type: str, tags: list[str], related_notes: list[str]) -> str:
    lines = ["---", f"type: {type}", f"date: {date.today().isoformat()}"]
    if tags:
        lines.append(f"tags: {json.dumps(tags)}")
    if related_notes:
        lines.append("related:")
        for note in related_notes:
            wrapped = note if note.startswith("[[") else f"[[{note}]]"
            lines.append(f'  - "{wrapped}"')
    lines.append("---")
    return "\n".join(lines)


def _resolve_filepath(vault: Path, type: str, title: str) -> Path:
    folder = vault / type
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{date.today().isoformat()}-{_slugify(title)}"
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

    return {
        "created": str(filepath.relative_to(vault)),
        "type": type,
        "title": title,
        "tags": tags,
        "related": related_notes,
        "path": str(filepath),
    }
