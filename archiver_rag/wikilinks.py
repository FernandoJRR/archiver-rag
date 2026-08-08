"""
Context-aware wikilink extractor for Obsidian markdown.

Design: compute code-region spans as [start, end) byte offsets into the original text,
then discard link matches that fall inside them. This avoids text-mutation bugs and lets
callers filter by section range without losing fence-open state from earlier in the file.

Masking order (each step excludes its region from subsequent steps):
  1. frontmatter  — excluded from code detection; 4-space-indented YAML would be
                    misread as indented code, deleting exactly the links we must keep.
  2. fenced code  — triple-backtick or triple-tilde, info string allowed, opener char
                    must match closer char, closer must be >= opener length.
                    Unclosed fence masks to EOF (surprising but correct — the renderer
                    treats everything below as code too).
  3. inline code  — a run of N backticks closes on the next run of exactly N backticks;
                    handles double-backtick spans without a special case.

Indented-code (4+ space) detection is intentionally omitted. A false negative there
leaves a phantom edge (status quo). A false positive deletes a real link, and nested
list items at 4- and 8-space indents are exactly where wikilinks live in practice.
All 6 measured phantoms in the vault are backtick/fence cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Span = tuple[int, int]  # [start, end)

# Require closing ]] and exclude newlines from target — prevents [[Foo swallowing a paragraph.
_LINK_RE = re.compile(r"!?\[\[([^\]\n|#]+)(?:#([^\]\n|]+))?(?:\|([^\]\n]+))?\]\]")

_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[^\n]*$", re.MULTILINE
)


def frontmatter_span(text: str) -> Span | None:
    """Return [0, end) of the YAML frontmatter block, or None if absent."""
    if not text.startswith("---"):
        return None
    # The opening --- must be at position 0; search for the closing --- on its own line.
    m = re.search(r"\n---[ \t]*(?:\n|$)", text, re.MULTILINE)
    if m:
        return (0, m.end())
    return None


def code_spans(text: str) -> list[Span]:
    """
    Return a sorted, non-overlapping list of [start, end) spans covering all
    fenced-code blocks and inline-code runs.  Frontmatter is excluded from detection.
    """
    fm = frontmatter_span(text)
    fm_end = fm[1] if fm else 0

    spans: list[Span] = []

    # ── fenced code ────────────────────────────────────────────────────────────
    # Walk fence markers; pair openers with the next compatible closer.
    pos = fm_end
    i = 0
    fences = list(_FENCE_RE.finditer(text))
    while i < len(fences):
        m = fences[i]
        if m.start() < fm_end:
            i += 1
            continue
        opener_char = m.group("fence")[0]
        opener_len = len(m.group("fence"))
        # Look for a closer: same char, same or longer, at column 0 (no extra indent).
        j = i + 1
        closed = False
        while j < len(fences):
            c = fences[j]
            if (
                c.group("fence")[0] == opener_char
                and len(c.group("fence")) >= opener_len
                and c.group("indent") == ""
            ):
                spans.append((m.start(), c.end()))
                i = j + 1
                closed = True
                break
            j += 1
        if not closed:
            # Unclosed fence: mask to EOF.
            spans.append((m.start(), len(text)))
            break
        # i was already advanced inside the loop

    # ── inline code ────────────────────────────────────────────────────────────
    # A run of N backticks opens; the next run of exactly N backticks closes.
    # We scan the text outside frontmatter and outside already-found fenced regions.
    backtick_re = re.compile(r"`+")
    pos = fm_end
    for bm in backtick_re.finditer(text, fm_end):
        start = bm.start()
        # Skip if inside a fenced span already found.
        if any(s <= start < e for s, e in spans):
            continue
        n = len(bm.group())
        # Find the matching closing run of exactly n backticks.
        close_re = re.compile(r"`{" + str(n) + r"}(?!`)")
        cm = close_re.search(text, bm.end())
        if cm:
            end = cm.end()
            spans.append((start, end))

    # Sort and merge overlapping spans.
    spans.sort()
    merged: list[Span] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    return merged


@dataclass(frozen=True)
class WikiLink:
    target: str  # The note stem (no [[, ]], #heading, or |alias)
    heading: str | None
    alias: str | None
    start: int  # offset of the opening [[ or ![[
    end: int  # offset just past the closing ]]
    raw: str  # the full matched text
    embed: bool  # True if ![[


def iter_wikilinks(
    text: str,
    *,
    skip_code: bool = True,
    masked: list[Span] | None = None,
) -> "Iterator[WikiLink]":
    """
    Yield WikiLink objects for every wikilink in text.

    skip_code=True  — skip links inside fenced/inline code (default; use for reading).
    skip_code=False — yield all links including inside code contexts. Use when building
                      a suppression list: if a masked link is wrongly excluded, auto_link
                      would consider it absent and write a duplicate. Permissive on write.

    masked — caller-supplied additional spans to skip (e.g. section-range filtering).
    """
    from typing import Iterator  # local import to avoid circular at module level

    if skip_code:
        code = code_spans(text)
    else:
        code = []

    extra = list(masked or [])
    skip_spans = sorted(code + extra)

    for m in _LINK_RE.finditer(text):
        start = m.start()
        end = m.end()
        if any(s <= start < e for s, e in skip_spans):
            continue
        yield WikiLink(
            target=m.group(1).strip(),
            heading=m.group(2).strip() if m.group(2) is not None else None,
            alias=m.group(3).strip() if m.group(3) is not None else None,
            start=start,
            end=end,
            raw=m.group(0),
            embed=m.group(0).startswith("!"),
        )


def extract_wikilinks(text: str, *, skip_code: bool = True) -> list[str]:
    """
    Drop-in replacement for WIKILINK_RE.findall(text): returns a list of target stems.
    Filters out links inside code spans when skip_code=True (the default).
    """
    return [wl.target for wl in iter_wikilinks(text, skip_code=skip_code)]
