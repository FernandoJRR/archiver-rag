import json
from pathlib import Path
from collections import defaultdict
from archiver_rag.const import WIKILINK_RE


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
            for link in [l.strip() for l in WIKILINK_RE.findall(content)]:
                if link != stem:
                    outgoing[stem].append(link)
                    incoming[link].append(stem)
        except Exception:
            continue
    return dict(outgoing), dict(incoming)
