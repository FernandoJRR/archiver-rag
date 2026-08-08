"""
Tests for the dead-target pruning feature of _append_links_section.

Pruning is opt-in: `valid_stems=None` (the default) disables it entirely,
so all 13 tests in test_linker_section.py pass unchanged.

Conservative pruning rules (keep on doubt):
  1. valid_stems is None              → keep everything (backward compat)
  2. target contains '/'              → keep (path-style links can't be resolved by stem)
  3. target not in valid_stems        → prune
"""
from archiver_rag.graph.linker import _append_links_section


# ── backward compatibility: valid_stems=None disables pruning ─────────────────

def test_no_valid_stems_keeps_all():
    """Default call (valid_stems=None) must never prune — backward compat."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead]]"
    result = _append_links_section(content, [], valid_stems=None)
    assert "[[Dead]]" in result


def test_empty_valid_stems_set_prunes_all():
    """An empty set means NO note is valid — all targets in Related are pruned."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead]]"
    result = _append_links_section(content, [], valid_stems=set())
    assert "[[Dead]]" not in result


# ── pruning: dead target removed, live target kept ────────────────────────────

def test_dead_target_pruned():
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead]]\n- [[Live]]"
    result = _append_links_section(content, [], valid_stems={"Live"})
    assert "[[Live]]" in result
    assert "[[Dead]]" not in result


def test_live_target_survives():
    content = "# Note\n\nBody.\n\n## Related\n- [[Alpha]]\n- [[Beta]]"
    result = _append_links_section(content, [], valid_stems={"Alpha", "Beta"})
    assert "[[Alpha]]" in result
    assert "[[Beta]]" in result


# ── conservative rule: path-style targets are never pruned ───────────────────

def test_path_style_target_kept_even_if_not_in_valid_stems():
    """[[folder/Note]] contains '/' — must survive even when valid_stems is empty."""
    content = "# Note\n\nBody.\n\n## Related\n- [[folder/Note]]"
    result = _append_links_section(content, [], valid_stems=set())
    assert "[[folder/Note]]" in result


# ── pruning interacts correctly with aliases and anchors ─────────────────────

def test_aliased_dead_target_pruned():
    """[[Dead|My Label]] — alias preserved on live; dropped entirely if dead."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead|My Label]]\n- [[Live|Keep]]"
    result = _append_links_section(content, [], valid_stems={"Live"})
    assert "[[Live|Keep]]" in result
    assert "Dead" not in result


def test_anchor_dead_target_pruned():
    """[[Dead#Section]] — anchor on a dead target must be pruned (target is 'Dead')."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead#Section]]\n- [[Live]]"
    result = _append_links_section(content, [], valid_stems={"Live"})
    assert "[[Live]]" in result
    assert "Dead" not in result


# ── no-op guard: prune-only writes to disk ────────────────────────────────────

def test_prune_without_additions_still_writes():
    """If something was pruned but nothing added, result must NOT be the original object."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead]]"
    result = _append_links_section(content, [], valid_stems=set())
    # Identity check — should NOT return original string (a write must happen)
    assert result is not content
    assert "[[Dead]]" not in result


def test_no_prune_no_addition_returns_original():
    """Nothing removed, nothing added → identical string object (no disk write)."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Live]]"
    result = _append_links_section(content, [], valid_stems={"Live"})
    assert result is content


# ── prune + add in the same call ─────────────────────────────────────────────

def test_prune_and_add_in_same_call():
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead]]\n- [[Live]]"
    result = _append_links_section(content, ["NewLink"], valid_stems={"Live", "NewLink"})
    assert "[[Live]]" in result
    assert "[[NewLink]]" in result
    assert "[[Dead]]" not in result


# ── sections after ## Related are never touched ───────────────────────────────

def test_sections_after_related_preserved_after_prune():
    content = (
        "# Note\n\nBody.\n\n"
        "## Related\n- [[Dead]]\n- [[Live]]\n\n"
        "## Notes\nImportant stuff."
    )
    result = _append_links_section(content, [], valid_stems={"Live"})
    assert "## Notes" in result, "## Notes section was destroyed"
    assert "Important stuff." in result, "Content after ## Related was destroyed"
    assert "[[Dead]]" not in result


# ── branch B (no ## Related section) ─────────────────────────────────────────

def test_no_section_empty_new_links_returns_original():
    """No Related section + empty new_links → no section is added, original returned."""
    content = "# Note\n\nBody."
    result = _append_links_section(content, [], valid_stems={"SomeNote"})
    assert "## Related" not in result
    assert result is content


def test_no_section_with_new_links_appends():
    """No Related section + non-empty new_links → section is appended as before."""
    content = "# Note\n\nBody."
    result = _append_links_section(content, ["Alpha"], valid_stems={"Alpha"})
    assert "## Related" in result
    assert "[[Alpha]]" in result
