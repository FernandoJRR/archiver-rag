import re
from pathlib import Path
from archiver_rag.core.embedder import embed
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path
from archiver_rag.const import WIKILINK_RE


def _get_existing_links(content: str) -> set[str]:
    """Extract all existing [[wikilinks]] from note content"""
    return {m.strip() for m in WIKILINK_RE.findall(content)}


def _append_links_section(content: str, new_links: list[str]) -> str:
    """
    Append a ## Related section at the bottom of the note.
    If section already exists, update it.
    """
    links_text = "\n".join(f"- [[{link}]]" for link in new_links)
    section = f"\n\n## Related\n{links_text}"

    # If Related section already exists, replace it
    related_pattern = re.compile(r'\n\n## Related\n.*$', re.DOTALL)
    if related_pattern.search(content):
        return related_pattern.sub(section, content)

    return content + section


def auto_link(filepath: str, min_score: float = 0.55, max_links: int = 5):
    """
    Called automatically by the watcher after a file is ingested.
    Finds semantically related notes and appends [[wikilinks]] to the note.
    """
    vault = Path(get_vault_path())
    note = Path(filepath)

    if not note.exists() or note.suffix != ".md":
        return

    content = note.read_text(encoding="utf-8", errors="ignore")

    if not content.strip():
        return

    # Get existing links to not duplicate
    existing_links = _get_existing_links(content)
    current_stem = note.stem

    # Embed the note content
    # Use first 500 words
    words = content.split()[:500]
    query_text = " ".join(words)
    query_vector = embed([query_text])[0]

    # Search for related chunks
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=20,  # fetch more, filter down
        include=["metadatas", "distances"]
    )

    metadatas = results.get("metadatas") or []
    distances = results.get("distances") or []

    if not metadatas or not metadatas[0]:
        return

    meta_list: list = metadatas[0]      # type: ignore[index]
    dist_list: list = distances[0]      # type: ignore[index]

    # Build candidates — deduplicate by source file
    seen_sources = set()
    candidates = []

    for meta, dist in zip(meta_list, dist_list):
        source = Path(meta["source"]).stem
        score = round(1 - (dist / 2), 3)

        # Skip self, already linked, low score, already seen this source
        if (
            source == current_stem
            or source in existing_links
            or score < min_score
            or source in seen_sources
        ):
            continue

        seen_sources.add(source)
        candidates.append((source, score))

    if not candidates:
        return

    # Sort by score, take top N
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_links = [source for source, _ in candidates[:max_links]]

    if not top_links:
        return

    # Append links to note
    updated_content = _append_links_section(content, top_links)
    note.write_text(updated_content, encoding="utf-8")
