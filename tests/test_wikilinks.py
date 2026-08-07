"""
Unit tests for archiver_rag.wikilinks.

Covers the cases that matter for correctness and the surprising edge cases that
would otherwise be "fixed" back to the broken state by a future reader.
"""
import pytest
from archiver_rag.wikilinks import (
    WikiLink,
    frontmatter_span,
    code_spans,
    iter_wikilinks,
    extract_wikilinks,
)


# ── frontmatter_span ──────────────────────────────────────────────────────────

def test_frontmatter_span_present():
    text = "---\ntype: decision\n---\n\nBody here."
    span = frontmatter_span(text)
    assert span is not None
    assert span[0] == 0
    # span ends after the closing ---\n; the blank line + body follow
    assert text[span[1]:].startswith("\nBody")


def test_frontmatter_span_absent():
    assert frontmatter_span("No frontmatter here.") is None


def test_frontmatter_span_does_not_exist_when_not_at_start():
    text = "Some text\n---\ntype: foo\n---\n"
    assert frontmatter_span(text) is None


# ── code_spans ────────────────────────────────────────────────────────────────

def test_fenced_code_basic():
    text = "before\n```\n[[phantom]]\n```\nafter"
    spans = code_spans(text)
    # phantom is inside the fence
    assert any(s <= text.index("[[phantom]]") < e for s, e in spans)


def test_fenced_code_with_info_string():
    text = "```python\n[[phantom]]\n```\n"
    spans = code_spans(text)
    assert any(s <= text.index("[[phantom]]") < e for s, e in spans)


def test_fenced_opener_closer_length_mismatch():
    # 4-backtick open, 3-backtick close — does NOT close the fence.
    # Everything after the opener is masked to EOF.
    text = "````\n[[phantom]]\n```\n[[also_phantom]]\n"
    spans = code_spans(text)
    assert any(s <= text.index("[[phantom]]") < e for s, e in spans)
    assert any(s <= text.index("[[also_phantom]]") < e for s, e in spans)


def test_fenced_backtick_inside_tilde_block():
    # A ``` inside a ~~~ block is not a fence closer.
    text = "~~~\n[[a]]\n```\n[[b]]\n~~~\n[[real]]"
    spans = code_spans(text)
    a_pos = text.index("[[a]]")
    b_pos = text.index("[[b]]")
    real_pos = text.index("[[real]]")
    assert any(s <= a_pos < e for s, e in spans)
    assert any(s <= b_pos < e for s, e in spans)
    assert not any(s <= real_pos < e for s, e in spans)


def test_unclosed_fence_masks_to_eof():
    # This is surprising: an unclosed fence makes everything below code.
    text = "```\n[[phantom]]\nno closing fence\n[[also_phantom]]"
    spans = code_spans(text)
    assert any(e == len(text) for _, e in spans), "unclosed fence must mask to EOF"
    assert any(s <= text.index("[[phantom]]") < e for s, e in spans)
    assert any(s <= text.index("[[also_phantom]]") < e for s, e in spans)


def test_inline_code_single_backtick():
    text = "See `[[phantom]]` for details."
    spans = code_spans(text)
    assert any(s <= text.index("[[phantom]]") < e for s, e in spans)


def test_inline_code_double_backtick():
    text = "See ``[[phantom]]`` here."
    spans = code_spans(text)
    assert any(s <= text.index("[[phantom]]") < e for s, e in spans)


def test_unmatched_stray_backtick_does_not_mask_forever():
    # A stray backtick with no closer should not mask the rest of the file.
    text = "A stray ` backtick here.\n[[real]]"
    spans = code_spans(text)
    real_pos = text.index("[[real]]")
    # real link must NOT be masked
    assert not any(s <= real_pos < e for s, e in spans)


def test_frontmatter_4space_indent_not_treated_as_code():
    # YAML related: block uses 4-space indented lines — must not be masked.
    text = "---\ntype: decision\nrelated:\n    - [[foo]]\n---\n\n[[bar]]"
    spans = code_spans(text)
    foo_pos = text.index("[[foo]]")
    bar_pos = text.index("[[bar]]")
    assert not any(s <= foo_pos < e for s, e in spans), "frontmatter link must not be masked"
    assert not any(s <= bar_pos < e for s, e in spans)


# ── extract_wikilinks ─────────────────────────────────────────────────────────

def test_extract_basic():
    assert extract_wikilinks("See [[Foo]] and [[Bar]].") == ["Foo", "Bar"]


def test_unterminated_link_not_extracted():
    # [[Foo without closing ]] must NOT be extracted.
    assert extract_wikilinks("[[Foo") == []


def test_link_with_heading():
    links = extract_wikilinks("See [[Foo#Section]].")
    assert links == ["Foo"]


def test_link_with_alias():
    links = extract_wikilinks("See [[Foo|display text]].")
    assert links == ["Foo"]


def test_link_with_heading_and_alias():
    links = extract_wikilinks("See [[Foo#Section|display]].")
    assert links == ["Foo"]


def test_embed_link():
    links = extract_wikilinks("![[image.png]]")
    assert links == ["image.png"]


def test_skip_code_true_removes_phantom_in_backtick():
    text = "Real: [[real]]. Code: `[[phantom]]`."
    links = extract_wikilinks(text, skip_code=True)
    assert "real" in links
    assert "phantom" not in links


def test_skip_code_false_keeps_all_links():
    text = "Real: [[real]]. Code: `[[phantom]]`."
    links = extract_wikilinks(text, skip_code=False)
    assert "real" in links
    assert "phantom" in links


def test_skip_code_removes_phantom_in_fence():
    text = "[[real]]\n```\n[[phantom]]\n```\n"
    links = extract_wikilinks(text, skip_code=True)
    assert "real" in links
    assert "phantom" not in links


def test_nested_list_items_extracted():
    # 4-space and 8-space list items must NOT be masked as indented code.
    text = "- [[top]]\n    - [[nested4]]\n        - [[nested8]]"
    links = extract_wikilinks(text)
    assert "top" in links
    assert "nested4" in links
    assert "nested8" in links


def test_frontmatter_links_extracted():
    text = "---\ntype: decision\nrelated:\n  - foo\n---\n\n[[bar]]"
    links = extract_wikilinks(text)
    assert "bar" in links


def test_newline_in_target_not_extracted():
    # A newline inside [[ breaks the match — prevent paragraph-eating.
    text = "[[Foo\nBar]]"
    assert extract_wikilinks(text) == []


# ── iter_wikilinks ─────────────────────────────────────────────────────────────

def test_iter_wikilinks_positions():
    text = "[[Foo]] and [[Bar]]"
    links = list(iter_wikilinks(text))
    assert len(links) == 2
    assert links[0].target == "Foo"
    assert links[0].start == 0
    assert links[1].target == "Bar"


def test_iter_wikilinks_heading_alias_parsed():
    text = "[[Note#Section|My Label]]"
    links = list(iter_wikilinks(text))
    assert len(links) == 1
    assert links[0].target == "Note"
    assert links[0].heading == "Section"
    assert links[0].alias == "My Label"


def test_iter_wikilinks_embed_flag():
    links = list(iter_wikilinks("![[image.png]]"))
    assert links[0].embed is True
    links2 = list(iter_wikilinks("[[note]]"))
    assert links2[0].embed is False


def test_iter_wikilinks_masked_span():
    text = "[[skip]] [[keep]]"
    skip_pos = text.index("[[skip]]")
    skip_end = skip_pos + len("[[skip]]")
    links = list(iter_wikilinks(text, skip_code=False, masked=[(skip_pos, skip_end)]))
    assert len(links) == 1
    assert links[0].target == "keep"
