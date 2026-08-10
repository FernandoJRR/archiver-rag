"""Tests for graph/terms.py — term extraction strategies."""

import pytest
from pathlib import Path
from archiver_rag.graph.terms import (
    _tokenize,
    _strip_related_section,
    _terms_by_tags_corpus,
    extract_terms,
)
from archiver_rag.utils import FOLDER_NOTE_NAME


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def write_note(vault: Path, rel: str, text: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


FRONTMATTER = "---\ntype: decision\ntags: [clustering, watcher]\n---\n"
RELATED = "\n## Related\n- [[some-other-note]]\n- [[another-note]]\n"


# ──────────────────────────────────────────────────────────────────────────────
# _tokenize
# ──────────────────────────────────────────────────────────────────────────────

def test_tokenize_lowercase():
    assert "clustering" in _tokenize("Clustering is important")


def test_tokenize_hyphenated_term_survives():
    assert "wikilink-resolver" in _tokenize("The wikilink-resolver handles this")


def test_tokenize_stopwords_dropped():
    tokens = _tokenize("the and for with from are was")
    assert tokens == []


def test_tokenize_min_length():
    # Single and two-char tokens are excluded
    tokens = _tokenize("a ab abc abcd")
    assert "a" not in tokens
    assert "ab" not in tokens
    assert "abc" in tokens


# ──────────────────────────────────────────────────────────────────────────────
# _strip_related_section
# ──────────────────────────────────────────────────────────────────────────────

def test_strip_related_removes_section():
    content = "Body text here.\n\n## Related\n- [[foo]]\n- [[bar]]\n"
    result = _strip_related_section(content)
    assert "[[foo]]" not in result
    assert "[[bar]]" not in result
    assert "Body text here" in result


def test_strip_related_noop_when_absent():
    content = "Just body text, no related section."
    result = _strip_related_section(content)
    assert result == content


def test_strip_related_leaves_other_headings():
    content = "## Background\nSome info.\n\n## Related\n- [[link]]\n\n## Notes\nMore.\n"
    result = _strip_related_section(content)
    assert "Background" in result
    assert "Notes" in result
    assert "[[link]]" not in result


# ──────────────────────────────────────────────────────────────────────────────
# _terms_by_tags_corpus (tag-frequency path, small folders)
# ──────────────────────────────────────────────────────────────────────────────

def test_tags_path_returns_terms():
    tags = ["clustering", "watcher", "watcher", "clustering", "indexing"]
    desc, dist = _terms_by_tags_corpus(tags, max_terms=4, mmr_lambda=0.5)
    assert "clustering" in desc or "watcher" in desc


def test_tags_path_empty_returns_empty():
    desc, dist = _terms_by_tags_corpus([], max_terms=4, mmr_lambda=0.5)
    assert desc == []
    assert dist == []


def test_tags_path_respects_max_terms():
    tags = [f"tag-{i}" for i in range(20)]
    desc, _ = _terms_by_tags_corpus(tags, max_terms=4, mmr_lambda=0.5)
    assert len(desc) <= 4


# ──────────────────────────────────────────────────────────────────────────────
# extract_terms — integration (uses embed(), needs real model)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_extract_terms_tags_path(tmp_path):
    """Small folder: should use tag frequency path (< term_extraction_min_notes)."""
    vault = make_vault(tmp_path)
    write_note(
        vault,
        "gotcha/spurious-delete.md",
        "---\ntype: gotcha\ntags: [watcher, delete, atomic-save]\n---\n# Spurious Delete\nThe watcher fires a delete.\n",
    )
    write_note(
        vault,
        "gotcha/chromadb-gotcha.md",
        "---\ntype: gotcha\ntags: [chromadb, cosine, configuration]\n---\n# ChromaDB\nUse cosine distance.\n",
    )
    desc, dist = extract_terms(vault, "gotcha", term_extraction_min_notes=4)
    assert len(desc) <= 6
    # Should not contain Related-section slugs (none present here, but verify no crash)
    assert isinstance(desc, list)


@pytest.mark.slow
def test_extract_terms_strips_related_section(tmp_path):
    """Terms from ## Related slugs must NOT appear in the description."""
    vault = make_vault(tmp_path)
    slug_terms = "some-very-specific-slug-term"
    for i in range(5):
        write_note(
            vault,
            f"decision/note-{i}.md",
            f"---\ntype: decision\ntags: [clustering]\n---\n# Note {i}\nThe watcher handles this.\n\n## Related\n- [[{slug_terms}]]\n",
        )
    desc, _ = extract_terms(vault, "decision", term_extraction_min_notes=4)
    # The slug should not dominate the description
    assert slug_terms not in desc


@pytest.mark.slow
def test_extract_terms_max_terms_respected(tmp_path):
    vault = make_vault(tmp_path)
    for i in range(6):
        write_note(
            vault,
            f"decision/note-{i}.md",
            f"---\ntags: [alpha-{i}, beta-{i}, gamma-{i}]\n---\n# Note\nContent about thing-{i} and widget-{i}.\n",
        )
    desc, _ = extract_terms(vault, "decision", term_extraction_min_notes=4, max_terms=4)
    assert len(desc) <= 4


@pytest.mark.slow
def test_extract_terms_empty_folder_returns_empty(tmp_path):
    vault = make_vault(tmp_path)
    (vault / "empty-folder").mkdir()
    desc, dist = extract_terms(vault, "empty-folder")
    assert desc == []
    assert dist == []
