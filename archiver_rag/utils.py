import json
import yaml
from pathlib import Path
from collections import defaultdict
from archiver_rag.wikilinks import extract_wikilinks


# Per-folder description sidecar (see graph/terms.py, vault/folder_notes.py).
# Visible so it can be read and edited from Obsidian, but never indexed, never
# auto-linked, and never part of the wikilink graph — see is_indexable_note.
FOLDER_NOTE_NAME = "_folder.md"


def get_vault_path() -> str:
    config_path = Path.home() / ".archiver-rag" / "config.json"
    return json.loads(config_path.read_text())["vault_path"]


def load_config() -> dict:
    """Runtime config from ~/.archiver-rag/config.json, or {} if missing/unreadable.

    Every default lives in code and the file is optional, so callers must read through
    config.get(key, default) rather than assuming a key is present.
    """
    try:
        config_path = Path.home() / ".archiver-rag" / "config.json"
        return json.loads(config_path.read_text())
    except Exception:
        return {}


def extract_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns ({}, content) when absent or malformed."""
    if not content.startswith("---"):
        return {}, content
    try:
        end = content.index("---", 3)
        fm_text = content[3:end].strip()
        body = content[end + 3 :].strip()
        return yaml.safe_load(fm_text) or {}, body
    except Exception:
        return {}, content


def is_hidden_path(path: Path) -> bool:
    """True if any path component starts with '.' (e.g. .trash, .obsidian, .git)."""
    return any(p.startswith(".") for p in path.parts)


def is_folder_note(path: Path) -> bool:
    """True if this is a folder-description sidecar rather than a real note."""
    return path.name == FOLDER_NOTE_NAME


def is_indexable_note(path: Path) -> bool:
    """True if `path` is a real vault note.

    A folder note ends in `.md` and lives in a visible directory, so neither the
    suffix check nor is_hidden_path excludes it — it has to be named out explicitly.
    Every place that enumerates notes goes through here so the three rules stay in
    one spot: markdown, not hidden, not a sidecar.
    """
    return (
        path.suffix == ".md" and not is_hidden_path(path) and not is_folder_note(path)
    )


def build_link_map(vault: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    outgoing: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    for note in vault.rglob("*.md"):
        if not is_indexable_note(note):
            continue
        try:
            content = note.read_text(encoding="utf-8", errors="ignore")
            stem = note.stem
            for link in extract_wikilinks(content):
                if link != stem:
                    outgoing[stem].append(link)
                    incoming[link].append(stem)
        except Exception:
            continue
    return dict(outgoing), dict(incoming)


def note_stems(vault: Path) -> set[str]:
    """Stems of every real note on disk. Excludes dot-prefixed paths and folder notes."""
    return {f.stem for f in vault.rglob("*.md") if is_indexable_note(f)}


def log(msg: str) -> None:
    """Print and flush.

    The service redirects stdout to /tmp/archiver-rag.log, so Python block-buffers it
    and `archiver-rag logs` can sit far behind reality — it showed an empty tail for
    events that had already been processed, which sent a debugging session chasing
    ghosts. Anything that runs inside the watcher process must log through here.
    """
    print(msg, flush=True)
