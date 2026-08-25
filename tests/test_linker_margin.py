"""
Tests for the margin-based candidate selection that replaced auto_link's fixed
per-run cap (see the "Desaturating the wikilink graph" work — measured density
0.66 / mean 25.2 links-per-note on the live vault before this change).

Two things under test:
  1. `select_related_candidates` (graph/linker.py) — the score-margin rule itself,
     shared by `auto_link` and the `relink` CLI command.
  2. `_append_links_section`'s `keep_targets` param — the rebuild-trimming half,
     independent of `valid_stems` dead-link pruning (see test_linker_prune.py).

Embedding/ChromaDB are mocked throughout — these are pure selection-logic tests,
not integration tests against a real index.
"""

from __future__ import annotations

import pytest

from archiver_rag.graph.linker import _append_links_section, select_related_candidates


# ── _append_links_section: keep_targets rebuild trimming ──────────────────────


def test_no_keep_targets_keeps_all():
    """Default call (keep_targets=None) must never trim — backward compat with
    the append-only behaviour every existing test in test_linker_section.py and
    test_linker_prune.py relies on."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Offmargin]]"
    result = _append_links_section(content, [], keep_targets=None)
    assert "[[Offmargin]]" in result


def test_off_margin_target_trimmed():
    content = "# Note\n\nBody.\n\n## Related\n- [[InMargin]]\n- [[OffMargin]]"
    result = _append_links_section(content, [], keep_targets={"InMargin"})
    assert "[[InMargin]]" in result
    assert "[[OffMargin]]" not in result


def test_empty_keep_targets_trims_all_except_exceptions():
    content = "# Note\n\nBody.\n\n## Related\n- [[Anything]]"
    result = _append_links_section(content, [], keep_targets=set())
    assert "[[Anything]]" not in result


def test_path_style_target_survives_keep_targets_trim():
    """Same conservative exception as valid_stems pruning: path-style links can't
    be resolved by stem, so keep_targets must never drop them."""
    content = "# Note\n\nBody.\n\n## Related\n- [[folder/Note]]"
    result = _append_links_section(content, [], keep_targets=set())
    assert "[[folder/Note]]" in result


def test_body_duplicated_target_survives_keep_targets_trim():
    """A link the user wrote by hand in the note body (outside ## Related) must
    survive keep_targets trimming even if the margin run doesn't currently rank
    it — this is the "deliberately duplicated, never drop" exception."""
    content = (
        "# Note\n\nSee [[Manual]] for background.\n\n"
        "## Related\n- [[Manual]]\n- [[Trimmed]]"
    )
    result = _append_links_section(content, [], keep_targets=set())
    assert "[[Manual]]" in result
    assert "[[Trimmed]]" not in result


def test_keep_targets_and_valid_stems_compose():
    """A link surviving one prune can still be trimmed by the other."""
    content = "# Note\n\nBody.\n\n## Related\n- [[Dead]]\n- [[OffMargin]]\n- [[Good]]"
    result = _append_links_section(
        content, [], valid_stems={"OffMargin", "Good"}, keep_targets={"Good"}
    )
    assert "[[Good]]" in result
    assert "[[Dead]]" not in result  # dropped by valid_stems (not a real file)
    assert "[[OffMargin]]" not in result  # dropped by keep_targets (below margin)


def test_keep_targets_additions_and_trim_in_same_call():
    content = "# Note\n\nBody.\n\n## Related\n- [[OffMargin]]\n- [[Stays]]"
    result = _append_links_section(
        content, ["NewLink"], keep_targets={"Stays", "NewLink"}
    )
    assert "[[Stays]]" in result
    assert "[[NewLink]]" in result
    assert "[[OffMargin]]" not in result


def test_no_op_when_nothing_pruned_or_added():
    content = "# Note\n\nBody.\n\n## Related\n- [[Stays]]"
    result = _append_links_section(content, [], keep_targets={"Stays"})
    assert result is content


# ── select_related_candidates: score-margin selection ──────────────────────────


class _FakeCollection:
    """Stands in for archiver_rag.core.db.collection. `rows` is an ordered list
    of (source_stem, distance) — order matters, matching ChromaDB's
    distance-ascending contract that select_related_candidates relies on to pick
    the first occurrence of a repeated source as its best chunk."""

    def __init__(self, rows: list[tuple[str, float]]):
        self.rows = rows

    def query(self, query_embeddings=None, n_results=40, include=None):
        metadatas = [{"source": f"{stem}.md"} for stem, _ in self.rows]
        distances = [d for _, d in self.rows]
        return {"metadatas": [metadatas], "distances": [distances]}


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    monkeypatch.setattr(
        "archiver_rag.graph.linker.embed", lambda texts: [[0.0] for _ in texts]
    )


def _score_to_dist(score: float) -> float:
    # base_score = 1 - dist/2  =>  dist = 2 * (1 - score)
    return 2 * (1 - score)


def test_margin_keeps_only_close_scores(monkeypatch):
    rows = [
        ("Top", _score_to_dist(0.90)),
        ("InMargin", _score_to_dist(0.87)),
        ("OffMargin", _score_to_dist(0.70)),
    ]
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection(rows))

    top_links, keep_targets = select_related_candidates(
        content="# Note\n\nBody text.",
        current_stem="Current",
        existing_links=set(),
        min_score=0.55,
        link_margin=0.05,
        max_total_links=15,
    )
    assert keep_targets == {"Top", "InMargin"}
    assert set(top_links) == {"Top", "InMargin"}


def test_self_excluded_from_candidates(monkeypatch):
    rows = [("Current", _score_to_dist(0.95)), ("Other", _score_to_dist(0.90))]
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection(rows))

    _top_links, keep_targets = select_related_candidates(
        content="# Note\n\nBody text.",
        current_stem="Current",
        existing_links=set(),
        min_score=0.55,
        link_margin=0.05,
        max_total_links=15,
    )
    assert "Current" not in keep_targets


def test_below_min_score_excluded_even_within_margin(monkeypatch):
    """min_score is a floor applied before the margin, not after — a candidate
    below it must never enter the pool even if it would fall within margin of
    another sub-floor candidate."""
    rows = [("Weak", _score_to_dist(0.50))]
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection(rows))

    top_links, keep_targets = select_related_candidates(
        content="# Note\n\nBody text.",
        current_stem="Current",
        existing_links=set(),
        min_score=0.55,
        link_margin=0.05,
        max_total_links=15,
    )
    assert keep_targets is None
    assert top_links == []


def test_no_candidates_returns_none_not_empty_set(monkeypatch):
    """No real candidate pool => keep_targets=None (don't trim), not an empty set
    (which would wipe every existing Related link) — 'when in doubt, keep'."""
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection([]))

    top_links, keep_targets = select_related_candidates(
        content="# Note\n\nBody text.",
        current_stem="Current",
        existing_links=set(),
        min_score=0.55,
        link_margin=0.05,
        max_total_links=15,
    )
    assert keep_targets is None
    assert top_links == []


def test_max_total_links_caps_even_within_margin(monkeypatch):
    rows = [(f"Note{i}", _score_to_dist(0.90)) for i in range(10)]  # all tied
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection(rows))

    _top_links, keep_targets = select_related_candidates(
        content="# Note\n\nBody text.",
        current_stem="Current",
        existing_links=set(),
        min_score=0.55,
        link_margin=0.05,
        max_total_links=3,
    )
    assert len(keep_targets) == 3


def test_already_linked_survivor_not_in_top_links_but_in_keep_targets(monkeypatch):
    """A candidate already present in ## Related shouldn't be re-added as a new
    line (top_links), but must still count toward keep_targets so it isn't
    trimmed by the rebuild."""
    rows = [("AlreadyLinked", _score_to_dist(0.90)), ("New", _score_to_dist(0.88))]
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection(rows))

    top_links, keep_targets = select_related_candidates(
        content="# Note\n\nBody text.",
        current_stem="Current",
        existing_links={"AlreadyLinked"},
        min_score=0.55,
        link_margin=0.05,
        max_total_links=15,
    )
    assert "AlreadyLinked" not in top_links
    assert "New" in top_links
    assert keep_targets == {"AlreadyLinked", "New"}


def test_empty_query_text_returns_no_candidates(monkeypatch):
    """A note whose body is empty after stripping frontmatter/Related must not
    call embed() with an empty string — short-circuits to (., None)."""
    monkeypatch.setattr("archiver_rag.graph.linker.collection", _FakeCollection([]))
    called = []
    monkeypatch.setattr(
        "archiver_rag.graph.linker.embed", lambda texts: called.append(texts) or [[0.0]]
    )

    top_links, keep_targets = select_related_candidates(
        content="---\ntitle: X\n---\n\n## Related\n- [[Foo]]",
        current_stem="Current",
        existing_links=set(),
        min_score=0.55,
        link_margin=0.05,
        max_total_links=15,
    )
    assert not called
    assert top_links == []
    assert keep_targets is None
