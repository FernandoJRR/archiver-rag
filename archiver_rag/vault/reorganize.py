import re
import shutil
from pathlib import Path
from archiver_rag.utils import get_vault_path, is_indexable_note


def _update_wikilinks(vault: Path, old_stem: str, new_stem: str):
    """Rewrite [[old_stem]], [[old_stem#heading]], and [[old_stem|alias]] across vault.

    Also rewrites bare names in YAML related: lists (lines like "  - old_stem").
    Left code-unaware deliberately: a rename should also update documented examples
    of the old link. See archiver_rag/wikilinks.py for context-aware extraction.
    """
    if old_stem == new_stem:
        return

    # Matches [[old_stem]], [[old_stem#heading]], [[old_stem|alias]],
    # [[old_stem#heading|alias]] — any combination of optional tail fragments.
    link_pattern = re.compile(
        rf"\[\[{re.escape(old_stem)}((?:#[^\]|]+)?(?:\|[^\]]+)?)\]\]"
    )
    # Bare name in YAML related: block: "  - old_stem" (exact word, no brackets)
    yaml_related_pattern = re.compile(
        rf"^([ \t]*-[ \t]+){re.escape(old_stem)}([ \t]*)$",
        re.MULTILINE,
    )

    for md_file in vault.rglob("*.md"):
        if not is_indexable_note(md_file):
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
            if old_stem not in content:
                continue

            new_content = link_pattern.sub(
                lambda m: f"[[{new_stem}{m.group(1)}]]", content
            )
            new_content = yaml_related_pattern.sub(
                lambda m: f"{m.group(1)}{new_stem}{m.group(2)}", new_content
            )
            if new_content != content:
                md_file.write_text(new_content, encoding="utf-8")
        except Exception:
            continue


def move_notes(moves: list[dict]) -> dict:
    vault = Path(get_vault_path())
    succeeded = []
    failed = []

    for move in moves:
        source = move.get("source")
        destination = move.get("destination")

        if not source or not destination:
            failed.append({"source": source, "error": "Missing source or destination"})
            continue

        src = vault / source
        dst = vault / destination

        # Security — prevent path traversal
        try:
            src.resolve().relative_to(vault.resolve())
            dst.resolve().relative_to(vault.resolve())
        except ValueError:
            failed.append({"source": source, "error": "Path outside vault boundary"})
            continue

        if not src.exists():
            failed.append({"source": source, "error": "File not found"})
            continue

        if dst.exists():
            failed.append(
                {
                    "source": source,
                    "error": f"Destination already exists: {destination}",
                }
            )
            continue

        try:
            # Create destination folder if needed
            dst.parent.mkdir(parents=True, exist_ok=True)

            # Move the file
            shutil.move(str(src), str(dst))

            # Update wikilinks if it's a markdown file
            if src.suffix == ".md":
                _update_wikilinks(vault, src.stem, dst.stem)

            succeeded.append({"source": source, "destination": destination})

        except Exception as e:
            failed.append({"source": source, "error": str(e)})

    if succeeded:
        from archiver_rag.core.ingest import prune_orphans

        prune_orphans(str(vault))

    return {
        "moved": len(succeeded),
        "failed": len(failed),
        "succeeded": succeeded,
        "errors": failed,
    }
