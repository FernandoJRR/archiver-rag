"""
Tests for sweep_dead_links() in vault/notes.py.

Isolated from delete_notes — tests the sweeping logic directly.
"""

from __future__ import annotations

from pathlib import Path


def _sweep(tmp_vault, stems: list[str]):
    from archiver_rag.vault.notes import sweep_dead_links

    return sweep_dead_links(tmp_vault.root, stems)


# ── no inbound links — no-op ──────────────────────────────────────────────────


def test_no_inbound_links_is_noop(tmp_vault):
    tmp_vault.write("orphan.md", "# Orphan\n\nNo one links here.")
    result = _sweep(tmp_vault, ["orphan"])
    assert result["swept"] == []
    assert result["errors"] == []


# ── dead target pruned, live target kept ──────────────────────────────────────


def test_dead_pruned_live_kept(tmp_vault):
    tmp_vault.write("live.md", "# Live")
    # linker links to both live (still on disk) and dead (already removed)
    tmp_vault.write("linker.md", "# Linker\n\n## Related\n- [[dead]]\n- [[live]]")
    result = _sweep(tmp_vault, ["dead"])
    assert "linker.md" in result["swept"]
    content = (tmp_vault.root / "linker.md").read_text()
    assert "[[dead]]" not in content
    assert "[[live]]" in content


# ── note in .trash not counted as valid ──────────────────────────────────────


def test_trash_note_not_valid(tmp_vault):
    # Simulate: 'gone' has already been moved to .trash before sweep is called
    (tmp_vault.root / ".trash").mkdir()
    (tmp_vault.root / ".trash" / "gone.md").write_text("# Gone", encoding="utf-8")
    tmp_vault.write("linker.md", "# Linker\n\n## Related\n- [[gone]]")
    result = _sweep(tmp_vault, ["gone"])
    content = (tmp_vault.root / "linker.md").read_text()
    assert "[[gone]]" not in content


# ── no-op when nothing to prune ──────────────────────────────────────────────


def test_noop_when_nothing_to_prune(tmp_vault):
    """If all targets in ## Related are still valid, no write should occur."""
    tmp_vault.write("real.md", "# Real")
    tmp_vault.write("linker.md", "# Linker\n\n## Related\n- [[real]]")
    # real.md still exists; sweep for a different (never-linked) stem
    result = _sweep(tmp_vault, ["unrelated"])
    assert result["swept"] == []


# ── multiple stems — collected in one pass ────────────────────────────────────


def test_multiple_stems_swept(tmp_vault):
    tmp_vault.write("linker.md", "# L\n\n## Related\n- [[a]]\n- [[b]]")
    # a and b are already gone (not on disk)
    result = _sweep(tmp_vault, ["a", "b"])
    content = (tmp_vault.root / "linker.md").read_text()
    assert "[[a]]" not in content
    assert "[[b]]" not in content
