from pathlib import Path
import re
import os
import yaml
import uuid

from archiver_rag.core.embedder import embed
from archiver_rag.core.chunker import chunk
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path, build_link_map
from archiver_rag.const import WIKILINK_RE

def _extract_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    try:
        end = content.index("---", 3)
        fm_text = content[3:end].strip()
        body = content[end + 3:].strip()
        return yaml.safe_load(fm_text) or {}, body
    except Exception:
        return {}, content

def _extract_wikilinks(content: str) -> list[str]:
    return [m.strip() for m in WIKILINK_RE.findall(content)]

def _extract_tags(frontmatter: dict, content: str) -> list[str]:
    tags: list[str] = []
    fm_tags = frontmatter.get("tags", [])
    if isinstance(fm_tags, list):
        tags.extend([str(t) for t in fm_tags])
    elif isinstance(fm_tags, str):
        tags.extend([t.strip() for t in fm_tags.split(",")])
    inline = re.findall(r'#([\w/-]+)', content)
    tags.extend(inline)
    return list(set(tags))

def _build_context_prefix(
    folder: str,
    links: list[str],
    tags: list[str],
    title: str
) -> str:
    parts = []
    if title:
        parts.append(f"Note: {title}")
    if folder and folder != ".":
        parts.append(f"Location: {folder}")
    if tags:
        parts.append(f"Tags: {', '.join(tags[:8])}")
    if links:
        parts.append(f"Links to: {', '.join(links[:8])}")
    return "\n".join(parts)

def _count_incoming_links(filepath: Path, vault: Path) -> int:
    _, incoming = build_link_map(vault)
    return len(incoming.get(filepath.stem, []))

def ingest_file(filepath: str):
    vault = get_vault_path()
    note = Path(filepath)

    if not note.exists() or note.suffix != ".md":
        return

    try:
        raw = note.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return

    if not raw.strip():
        return

    # Parse context structure
    frontmatter, body = _extract_frontmatter(raw)
    links = _extract_wikilinks(raw)
    tags = _extract_tags(frontmatter, body)
    title = str(frontmatter.get("title", note.stem))

    try:
        folder = str(note.parent.relative_to(vault))
    except ValueError:
        folder = "."

    # Step 1: Build contextual prefix
    prefix = _build_context_prefix(folder, links, tags, title)

    # Chunk body
    chunks = chunk(body)
    if not chunks:
        return

    # Prepend prefix to each chunk before embedding
    contextualized = [
        f"{prefix}\n\n{chunk}" if prefix else chunk
        for chunk in chunks
    ]

    vectors = embed(contextualized)
    filename = note.name

    # Step 2: count incoming links for hub boost
    incoming_count = _count_incoming_links(note, Path(vault))

    #------------------------------------------------
    collection.delete(where={"source": filename})

    collection.add(
        ids=[str(uuid.uuid4()) for _ in chunks],
        embeddings=vectors,
        documents=chunks,
        metadatas=[{
            "source": filename,
            "path": filepath,
            "folder": folder,
            "tags": ",".join(tags),
            "links": ",".join(links),
            "incoming_count": incoming_count,
            "title": title
        } for _ in chunks]
    )

    print(f"Indexed {len(chunks)} chunks from {filename}")

def ingest_vault(vault_path: str):
    for root, dirs, files in os.walk(vault_path):
        # Skip Obsidian's hidden folder
        dirs[:] = [d for d in dirs if d != ".obsidian"]

        for file in files:
            if file.endswith(".md"):
                ingest_file(os.path.join(root, file))

if __name__ == "__main__":
    import sys
    vault_path = sys.argv[1]
    print(f"Ingesting vault on: {vault_path}")
    ingest_vault(vault_path)
    print(f"Finished!")
