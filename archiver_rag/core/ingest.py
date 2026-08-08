from pathlib import Path
import re
import os
import yaml
import uuid

from archiver_rag.core.embedder import embed
from archiver_rag.core.chunker import chunk
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path, build_link_map
from archiver_rag.wikilinks import extract_wikilinks

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
    links = extract_wikilinks(raw)
    tags = _extract_tags(frontmatter, body)
    title = str(frontmatter.get("title", note.stem))

    try:
        folder = str(note.parent.relative_to(vault))
    except ValueError:
        folder = "."

    note_type = str(frontmatter.get("type") or Path(folder).name or "note").strip()

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

    try:
        source = str(note.relative_to(Path(vault)))
    except ValueError:
        source = note.name

    # Step 2: count incoming links for hub boost
    incoming_count = _count_incoming_links(note, Path(vault))
    mtime = int(os.path.getmtime(filepath))

    #------------------------------------------------
    collection.delete(where={"source": source})

    collection.add(
        ids=[str(uuid.uuid4()) for _ in chunks],
        embeddings=vectors,
        documents=chunks,
        metadatas=[{
            "source": source,
            "path": filepath,
            "folder": folder,
            "type": note_type,
            "tags": ",".join(tags),
            "links": ",".join(links),
            "incoming_count": incoming_count,
            "title": title,
            "mtime": mtime,
        } for _ in chunks]
    )

    print(f"Indexed {len(chunks)} chunks from {source}")

def prune_orphans(vault_path: str) -> int:
    """Delete chunks whose source file no longer exists on disk. Returns count of pruned sources."""
    vault = Path(vault_path)
    result = collection.get(include=["metadatas"])
    if not result["metadatas"]:
        return 0

    seen: set[str] = set()
    orphaned: list[str] = []
    for meta in result["metadatas"]:
        src = meta.get("source")
        if not src or src in seen:
            continue
        seen.add(src)
        if not (vault / src).exists():
            orphaned.append(src)

    for src in orphaned:
        collection.delete(where={"source": src})
        print(f"Pruned orphan: {src}")

    return len(orphaned)

def ingest_vault(vault_path: str):
    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for file in files:
            if file.endswith(".md"):
                ingest_file(os.path.join(root, file))

    prune_orphans(vault_path)

def sync_vault(vault_path: str) -> dict:
    """Ingest only notes that are missing from or staler than the ChromaDB index."""
    result = collection.get(include=["metadatas"])
    indexed: dict[str, float] = {}
    for meta in result["metadatas"]:
        source = meta.get("source")
        if not source or source in indexed:
            continue
        mtime = meta.get("mtime")
        if mtime is not None:
            indexed[source] = float(mtime)

    ingested = 0
    up_to_date = 0

    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file in files:
            if not file.endswith(".md"):
                continue
            filepath = os.path.join(root, file)
            try:
                source = str(Path(filepath).relative_to(Path(vault_path)))
            except ValueError:
                source = file

            file_mtime = int(os.path.getmtime(filepath))

            if source not in indexed or file_mtime > indexed[source]:
                ingest_file(filepath)
                ingested += 1
            else:
                up_to_date += 1

    pruned = prune_orphans(vault_path)
    return {"indexed": ingested, "up_to_date": up_to_date, "pruned": pruned}

if __name__ == "__main__":
    import sys
    vault_path = sys.argv[1]
    print(f"Ingesting vault on: {vault_path}")
    ingest_vault(vault_path)
    print(f"Finished!")
