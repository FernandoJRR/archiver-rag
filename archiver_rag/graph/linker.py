import re
from pathlib import Path
from archiver_rag.core.embedder import embed
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path, note_stems
from archiver_rag.wikilinks import extract_wikilinks


def _get_existing_links(content: str) -> set[str]:
    """Extract all existing [[wikilinks]] from note content.
    Deliberately permissive (skip_code=False): if the masker wrongly classifies a real
    link as code, auto_link would consider it absent and write a duplicate into the note.
    """
    return set(extract_wikilinks(content, skip_code=False))


_RELATED_HEADING_RE = re.compile(r"^[ \t]{0,3}#{2,6}[ \t]+Related[ \t]*$", re.MULTILINE)
_ANY_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]", re.MULTILINE)


def _find_related_section(content: str) -> tuple[int, int, int] | None:
    """
    Return (heading_start, body_start, body_end) for the first ## Related section,
    where body_end is the start of the next heading at same-or-higher level (or EOF).
    Returns None if no ## Related heading exists.
    """
    m = _RELATED_HEADING_RE.search(content)
    if m is None:
        return None

    heading_level = len(m.group(0).lstrip().split()[0])  # count # chars
    body_start = m.end()
    # Advance past the trailing newline of the heading line
    if body_start < len(content) and content[body_start] == "\n":
        body_start += 1

    # Find next heading at level <= heading_level (i.e. ## or higher-level)
    body_end = len(content)
    for nm in _ANY_HEADING_RE.finditer(content, body_start):
        level = len(nm.group(0).lstrip()) - 1  # strip leading spaces, count #
        # _ANY_HEADING_RE matches "# " not "#" so group(0) ends with a space
        # Count the # chars only
        stripped = nm.group(0).lstrip()
        level = len(stripped) - len(stripped.lstrip("#"))
        if level <= heading_level:
            body_end = nm.start()
            break

    return (m.start(), body_start, body_end)


def _append_links_section(
    content: str,
    new_links: list[str],
    valid_stems: set[str] | None = None,
) -> str:
    """
    Append or update the ## Related section, merging with any existing links.

    - Existing [[Foo|alias]] and [[Foo#Section]] are kept verbatim (no flattening).
    - Deduplication compares on target stem so an aliased link suppresses a bare one.
    - Uses slicing + string concatenation, never re.sub, so backslashes in stems are safe.
    - A heading at any level (##, ###, …) named "Related" is recognised (tolerates
      trailing spaces and ### Related).
    - Sections after ## Related are preserved.

    Pruning: when `valid_stems` is provided, existing links inside ## Related whose
    target is NOT in `valid_stems` are silently dropped. Three conservative rules —
    when in doubt, keep:
      1. `valid_stems is None` — no pruning at all (default, backward-compatible).
      2. Target contains '/' (path-style link like [[folder/Note]]) — keep. Path stems
         never match a plain Path.stem, so resolving them would require different logic.
      3. Otherwise prune only if `wl.target not in valid_stems`.
    """
    from archiver_rag.wikilinks import iter_wikilinks

    location = _find_related_section(content)

    if location is not None:
        heading_start, body_start, body_end = location
        body = content[body_start:body_end]

        # Collect existing wikilinks in the body verbatim (skip_code=False: permissive).
        # When valid_stems is given, prune dead targets conservatively (see docstring).
        existing_raw: list[str] = []
        existing_targets: set[str] = set()
        pruned = False
        for wl in iter_wikilinks(body, skip_code=False):
            if (
                valid_stems is not None
                and "/" not in wl.target
                and wl.target not in valid_stems
            ):
                pruned = True
                continue
            existing_raw.append(wl.raw.lstrip("!"))  # drop embed !
            existing_targets.add(wl.target)

        # Only append links whose target isn't already present
        additions = [l for l in new_links if l not in existing_targets]

        # No-op: nothing pruned AND nothing added — return the original object so
        # auto_link's identity check skips the disk write.
        if not pruned and not additions:
            return content

        # Rebuild body: keep surviving existing lines verbatim, append new ones
        existing_lines = [f"- {raw}" for raw in existing_raw]
        new_lines = [f"- [[{link}]]" for link in additions]
        new_body = "\n".join(existing_lines + new_lines)

        before = content[:heading_start]
        after = content[body_end:]
        # Determine heading text from original (preserve level and spacing)
        heading_line = content[heading_start:body_start].rstrip("\n")
        return before + heading_line + "\n" + new_body + after

    # No ## Related section yet — append one only if there is something to add.
    # (With pruning active and no section, there is nothing to prune either.)
    if not new_links:
        return content
    links_text = "\n".join(f"- [[{link}]]" for link in new_links)
    separator = "\n\n" if not content.endswith("\n") else "\n"
    return content + separator + "## Related\n" + links_text


def auto_link(filepath: str, min_score: float = 0.55, max_links: int = 5):
    """
    Called automatically by the watcher after a file is ingested.
    Finds semantically related notes and appends [[wikilinks]] to the note.
    Also prunes dead targets from ## Related (targets with no file on disk).
    """
    vault = Path(get_vault_path())
    note = Path(filepath)

    if not note.exists() or note.suffix != ".md":
        return

    content = note.read_text(encoding="utf-8", errors="ignore")

    if not content.strip():
        return

    # Build valid-stems set for pruning (once per call, reused by _append_links_section)
    valid = note_stems(vault)

    # Get existing links to not duplicate
    existing_links = _get_existing_links(content)
    current_stem = note.stem

    # Attempt to find new candidates via semantic search.
    # If the search yields nothing, top_links stays empty and we still run
    # _append_links_section so that dead-target pruning happens regardless.
    top_links: list[str] = []

    # Embed the note content — use first 500 words
    words = content.split()[:500]
    query_text = " ".join(words)
    query_vector = embed([query_text])[0]

    # Search for related chunks
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=20,  # fetch more, filter down
        include=["metadatas", "distances"],
    )

    metadatas = results.get("metadatas") or []
    distances = results.get("distances") or []

    if metadatas and metadatas[0]:
        meta_list: list = metadatas[0]  # type: ignore[index]
        dist_list: list = distances[0]  # type: ignore[index]

        # Build candidates — deduplicate by source file
        seen_sources: set[str] = set()
        candidates: list[tuple[str, float]] = []

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

        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            top_links = [source for source, _ in candidates[:max_links]]

    # _append_links_section returns the original string object when nothing changed;
    # the identity check here is the sole guard against unnecessary disk writes.
    updated_content = _append_links_section(content, top_links, valid)
    if updated_content is content:
        return
    note.write_text(updated_content, encoding="utf-8")
