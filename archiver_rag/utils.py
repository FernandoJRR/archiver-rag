import json
from pathlib import Path
from collections import defaultdict
from archiver_rag.wikilinks import extract_wikilinks


def get_vault_path() -> str:
    config_path = Path.home() / ".archiver-rag" / "config.json"
    return json.loads(config_path.read_text())["vault_path"]


def build_link_map(vault: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    outgoing: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    for note in vault.rglob("*.md"):
        if any(p.startswith(".") for p in note.parts):
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
    """Stems of every real note on disk. Excludes dot-prefixed paths (.obsidian, .git)."""
    return {
        f.stem
        for f in vault.rglob("*.md")
        if not any(p.startswith(".") for p in f.parts)
    }


def is_hidden_path(path: Path) -> bool:
    """True if any path component starts with '.' (e.g. .trash, .obsidian, .git)."""
    return any(p.startswith(".") for p in path.parts)


def log(msg: str) -> None:
    """Print and flush.

    The service redirects stdout to /tmp/archiver-rag.log, so Python block-buffers it
    and `archiver-rag logs` can sit far behind reality — it showed an empty tail for
    events that had already been processed, which sent a debugging session chasing
    ghosts. Anything that runs inside the watcher process must log through here.
    """
    print(msg, flush=True)
