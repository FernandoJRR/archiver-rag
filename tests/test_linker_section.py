"""
Characterization tests for _append_links_section in graph/linker.py.

Tests marked xfail(strict=True) document the four data-loss bugs that existed
before the rewrite. After the fix they should XPASS and the markers are removed.
Tests without xfail document currently-correct behaviour that must not regress.
"""

import pytest
from archiver_rag.graph.linker import _append_links_section


# ── currently correct behaviour (must not regress) ────────────────────────────


def test_no_section_appends():
    content = "# Note\n\nSome body text."
    result = _append_links_section(content, ["Foo", "Bar"])
    assert "## Related" in result
    assert "[[Foo]]" in result
    assert "[[Bar]]" in result


def test_existing_section_merges_new_links():
    content = "# Note\n\nBody.\n\n## Related\n- [[Existing]]"
    result = _append_links_section(content, ["NewLink"])
    assert "[[Existing]]" in result
    assert "[[NewLink]]" in result


def test_idempotent_same_links():
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo]]"
    result = _append_links_section(content, ["Foo"])
    assert result.count("[[Foo]]") == 1


def test_empty_new_links_no_duplicate_section():
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo]]"
    result = _append_links_section(content, [])
    assert result.count("## Related") == 1


# ── bug 1: DOTALL .*? eats content after ## Related ──────────────────────────


def test_content_after_related_section_preserved():
    """Sections after ## Related must not be destroyed."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo]]\n\n## Notes\nImportant stuff."
    result = _append_links_section(content, ["Bar"])
    assert "## Notes" in result, "## Notes section was destroyed"
    assert "Important stuff." in result, "Content after ## Related was destroyed"


# ── bug 2: backslash in stem causes re.error ──────────────────────────────────


def test_backslash_in_new_link_does_not_raise():
    """A link target containing a backslash (e.g. Windows path) must not raise re.error."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Existing]]"
    # Should not raise
    result = _append_links_section(content, [r"Some\Path"])
    assert r"Some\Path" in result


# ── bug 3: aliases and anchors preserved across passes ───────────────────────


def test_alias_preserved_on_second_pass():
    """[[Foo|My Label]] must not be flattened to [[Foo]] by a subsequent call."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo|My Label]]"
    result = _append_links_section(content, ["Bar"])
    assert "[[Foo|My Label]]" in result, "alias was flattened"


def test_heading_anchor_preserved_on_second_pass():
    """[[Foo#Section]] must survive a subsequent auto_link pass."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo#Section]]"
    result = _append_links_section(content, ["Bar"])
    assert "[[Foo#Section]]" in result, "heading anchor was stripped"


def test_aliased_link_suppresses_bare_duplicate():
    """If [[Foo|label]] is already in the section, [[Foo]] must not also be added."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo|My Label]]"
    result = _append_links_section(content, ["Foo"])
    assert result.count("[[Foo") == 1, "aliased link was duplicated as bare link"


# ── bug 4: single-newline separation before ## Related ───────────────────────


def test_single_newline_before_related_no_duplicate():
    """If the body ends with only one newline before ## Related, must not duplicate the section."""
    content = "# Note\n\nBody.\n## Related\n- [[Foo]]"
    result = _append_links_section(content, ["Bar"])
    assert result.count("## Related") == 1, "## Related section was duplicated"
    assert "[[Bar]]" in result


# ── no-op write guard ─────────────────────────────────────────────────────────


def test_returns_original_when_no_changes():
    """When all new_links are already present, return the identical string object."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Foo]]"
    result = _append_links_section(content, ["Foo"])
    assert result is content or result == content, "unnecessary rewrite detected"
