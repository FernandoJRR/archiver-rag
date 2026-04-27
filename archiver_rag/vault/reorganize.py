import re
import shutil
from pathlib import Path
from archiver_rag.utils import get_vault_path


def _update_wikilinks(vault: Path, old_stem: str, new_stem: str):
    """Rewrite [[old_stem]] to [[new_stem]] across entire vault"""
    if old_stem == new_stem:
        return

    pattern = re.compile(
        rf'\[\[{re.escape(old_stem)}(\|[^\]]+)?\]\]'
    )

    for md_file in vault.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if old_stem not in content:
                continue

            def replace_link(m):
                alias = m.group(1) or ""
                return f"[[{new_stem}{alias}]]"

            new_content = pattern.sub(replace_link, content)
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
            failed.append({
                "source": source,
                "error": "Missing source or destination"
            })
            continue

        src = vault / source
        dst = vault / destination

        # Security — prevent path traversal
        try:
            src.resolve().relative_to(vault.resolve())
            dst.resolve().relative_to(vault.resolve())
        except ValueError:
            failed.append({
                "source": source,
                "error": "Path outside vault boundary"
            })
            continue

        if not src.exists():
            failed.append({
                "source": source,
                "error": "File not found"
            })
            continue

        if dst.exists():
            failed.append({
                "source": source,
                "error": f"Destination already exists: {destination}"
            })
            continue

        try:
            # Create destination folder if needed
            dst.parent.mkdir(parents=True, exist_ok=True)

            # Move the file
            shutil.move(str(src), str(dst))

            # Update wikilinks if it's a markdown file
            if src.suffix == ".md":
                _update_wikilinks(vault, src.stem, dst.stem)

            succeeded.append({
                "source": source,
                "destination": destination
            })

        except Exception as e:
            failed.append({
                "source": source,
                "error": str(e)
            })

    return {
        "moved": len(succeeded),
        "failed": len(failed),
        "succeeded": succeeded,
        "errors": failed
    }
