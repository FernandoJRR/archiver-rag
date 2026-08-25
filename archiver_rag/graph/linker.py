import re
from pathlib import Path
from archiver_rag.core.embedder import embed
from archiver_rag.core.db import collection
from archiver_rag.utils import (
    get_vault_path,
    note_stems,
    is_indexable_note,
    extract_frontmatter,
    strip_related_section,
)
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
    keep_targets: set[str] | None = None,
) -> str:
    """
    Append or update the ## Related section, merging with any existing links.

    - Existing [[Foo|alias]] and [[Foo#Section]] are kept verbatim (no flattening).
    - Deduplication compares on target stem so an aliased link suppresses a bare one.
    - Uses slicing + string concatenation, never re.sub, so backslashes in stems are safe.
    - A heading at any level (##, ###, …) named "Related" is recognised (tolerates
      trailing spaces and ### Related).
    - Sections after ## Related are preserved.

    Dead-link pruning: when `valid_stems` is provided, existing links inside ## Related
    whose target is NOT in `valid_stems` are silently dropped. Three conservative rules —
    when in doubt, keep:
      1. `valid_stems is None` — no pruning at all (default, backward-compatible).
      2. Target contains '/' (path-style link like [[folder/Note]]) — keep. Path stems
         never match a plain Path.stem, so resolving them would require different logic.
      3. Otherwise prune only if `wl.target not in valid_stems`.

    Rebuild trimming: when `keep_targets` is provided (the margin-selected candidate
    set from auto_link — see graph/linker.py::auto_link), existing links inside
    ## Related whose target is NOT in `keep_targets` are also dropped, unless:
      1. `keep_targets is None` — no trimming at all (default, backward-compatible;
         this is what makes the section append-only rather than rebuilt).
      2. Target contains '/' — same conservative path-style exception as above.
      3. Target also appears as a wikilink somewhere in the note body *outside*
         ## Related — a user-authored, deliberately duplicated link is never trimmed
         just because it fell outside this run's margin.
    `valid_stems` and `keep_targets` are independent and compose: a link surviving
    one prune can still be trimmed by the other.
    """
    from archiver_rag.wikilinks import iter_wikilinks

    location = _find_related_section(content)

    if location is not None:
        heading_start, body_start, body_end = location
        body = content[body_start:body_end]

        # Links appearing in the note body outside ## Related — protects
        # keep_targets trimming from removing a link the user duplicated by hand.
        body_targets: set[str] = set()
        if keep_targets is not None:
            outside = content[:heading_start] + content[body_end:]
            body_targets = {wl.target for wl in iter_wikilinks(outside, skip_code=False)}

        # Collect existing wikilinks in the body verbatim (skip_code=False: permissive).
        # When valid_stems/keep_targets are given, prune conservatively (see docstring).
        existing_raw: list[str] = []
        existing_targets: set[str] = set()
        pruned = False
        for wl in iter_wikilinks(body, skip_code=False):
            is_dead = (
                valid_stems is not None
                and "/" not in wl.target
                and wl.target not in valid_stems
            )
            is_trimmed = (
                keep_targets is not None
                and "/" not in wl.target
                and wl.target not in keep_targets
                and wl.target not in body_targets
            )
            if is_dead or is_trimmed:
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


def _get_link_margin_config() -> tuple[float, int]:
    """Return (link_margin, max_total_links) for auto_link's candidate selection.

    Unlike auto_cluster/auto_describe, these are tuning floats, not gating flags —
    load_config()'s {} on any read error resolves via .get() to the same defaults
    below, so (unlike watcher.py's _get_cluster_config) there is no unsafe-default
    hazard in going through load_config() here.
    """
    from archiver_rag.utils import load_config

    advanced = load_config().get("advanced", {})
    return (
        float(advanced.get("link_margin", 0.05)),
        int(advanced.get("max_total_links", 15)),
    )


def select_related_candidates(
    content: str,
    current_stem: str,
    existing_links: set[str],
    min_score: float,
    link_margin: float,
    max_total_links: int,
) -> tuple[list[str], set[str] | None]:
    """Embed `content` (Related-stripped) and margin-select its related notes.

    Returns (top_links, keep_targets):
      - top_links: candidates not already in `existing_links` — new lines to add.
      - keep_targets: the full survivor set (new + already-linked), for
        `_append_links_section`'s rebuild trimming. `None` when there is no real
        candidate pool to judge against — "when in doubt, keep" (matches
        `_append_links_section`'s own pruning contract), not an aggressive wipe.

    Candidate selection (measured on the live vault — see the vault note
    *wikilink-graph-desaturated-margin-based-linking-replaces-per-run-cap*): at
    min_score=0.55, ~30 of ~80 vault notes clear the floor for any given query, so
    a plain top-N cap always saturates. Instead, keep every candidate within
    `link_margin` of the *top* candidate's score — genuinely adaptive: a note in a
    dense neighbourhood keeps more links, an isolated note keeps few.
    `max_total_links` is a safety ceiling above the observed range, not the primary
    control.

    Shared by `auto_link` (single note, called by the watcher) and the `relink` CLI
    command (whole-vault repair pass) so the two selection rules never drift apart.
    """
    # Without stripping, the query would include the note's own growing list of
    # neighbour filenames, pulling the search toward whatever those neighbours
    # already link to rather than what this note is actually about — a feedback loop.
    _fm, body = extract_frontmatter(content)
    query_text = " ".join(strip_related_section(body).split()[:500])
    if not query_text.strip():
        return [], None
    query_vector = embed([query_text])[0]

    # Fetch a wide pool (40, not just max_total_links): the margin rule needs a
    # score for every *currently linked* note too, to decide whether it still
    # belongs — not only for first-time candidates.
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=40,
        include=["metadatas", "distances"],
    )

    metadatas = results.get("metadatas") or []
    distances = results.get("distances") or []

    # Best score per source (results arrive ordered by distance ascending, so the
    # first occurrence of a source is already its closest chunk).
    scored: dict[str, float] = {}
    if metadatas and metadatas[0]:
        meta_list: list = metadatas[0]  # type: ignore[index]
        dist_list: list = distances[0]  # type: ignore[index]

        for meta, dist in zip(meta_list, dist_list):
            source = Path(meta["source"]).stem
            score = round(1 - (dist / 2), 3)

            if source == current_stem or score < min_score:
                continue
            if source not in scored:
                scored[source] = score

    if not scored:
        return [], None

    top_score = max(scored.values())
    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    kept = [s for s, sc in ranked if sc >= top_score - link_margin][:max_total_links]
    keep_targets = set(kept)
    # Only genuinely new links need to be written as additions — survivors already
    # present in ## Related are handled by keep_targets, not re-added.
    top_links = [s for s in kept if s not in existing_links]
    return top_links, keep_targets


def auto_link(
    filepath: str,
    min_score: float = 0.55,
    link_margin: float | None = None,
    max_total_links: int | None = None,
):
    """
    Called automatically by the watcher after a file is ingested.
    Finds semantically related notes and rebuilds the note's ## Related section.
    Also prunes dead targets from ## Related (targets with no file on disk).
    """
    if link_margin is None or max_total_links is None:
        cfg_margin, cfg_max_total = _get_link_margin_config()
        link_margin = cfg_margin if link_margin is None else link_margin
        max_total_links = cfg_max_total if max_total_links is None else max_total_links
    vault = Path(get_vault_path())
    note = Path(filepath)

    if not note.exists() or not is_indexable_note(note):
        return

    content = note.read_text(encoding="utf-8", errors="ignore")

    if not content.strip():
        return

    # Build valid-stems set for pruning (once per call, reused by _append_links_section)
    valid = note_stems(vault)

    # Get existing links to not duplicate
    existing_links = _get_existing_links(content)
    current_stem = note.stem

    top_links, keep_targets = select_related_candidates(
        content, current_stem, existing_links, min_score, link_margin, max_total_links
    )

    # _append_links_section returns the original string object when nothing changed;
    # the identity check here is the sole guard against unnecessary disk writes.
    updated_content = _append_links_section(content, top_links, valid, keep_targets)
    if updated_content is content:
        return
    note.write_text(updated_content, encoding="utf-8")
