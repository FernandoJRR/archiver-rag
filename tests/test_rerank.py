"""
Tests for hub_boost scaling in graph/rerank.py.

hub_boost saturates at HUB_BOOST_SATURATION_INCOMING inbound links (12, re-centered
near the post-desaturation p90 — see the module-level comment in rerank.py and the
vault note wikilink-graph-desaturated-margin-based-linking-replaces-per-run-cap).
Originally 5, calibrated when auto_link's old per-run append-only cap had driven
mean inbound links to 25.7 (90% of notes >= 5) — that made hub_boost a near-constant
+0.10 for nearly every result. No graph_boost/build_link_map involved here, so
vault_path is irrelevant to hub_boost and left out of these cases by using
query_note=None (skips the link-map lookups entirely).
"""

from __future__ import annotations

from archiver_rag.graph.rerank import HUB_BOOST_MAX, HUB_BOOST_SATURATION_INCOMING, rerank


def _rerank_one(incoming_count: int, dist: float = 0.2, vault_path: str = "/tmp/unused"):
    docs = ["some content"]
    metas = [{"source": "Note.md", "incoming_count": incoming_count}]
    dists = [dist]
    return rerank(docs, metas, dists, vault_path=vault_path, min_score=0.0)[0]


def test_hub_boost_zero_at_zero_incoming():
    result = _rerank_one(0)
    assert result["hub_boost"] == 0.0


def test_hub_boost_scales_below_saturation():
    low = _rerank_one(2)
    high = _rerank_one(6)
    assert 0.0 < low["hub_boost"] < high["hub_boost"] < HUB_BOOST_MAX


def test_hub_boost_saturates_at_threshold():
    at_threshold = _rerank_one(HUB_BOOST_SATURATION_INCOMING)
    assert at_threshold["hub_boost"] == HUB_BOOST_MAX


def test_hub_boost_capped_above_threshold():
    """A real hub (e.g. 25 inbound, the post-repair vault max) must not exceed the cap."""
    way_above = _rerank_one(HUB_BOOST_SATURATION_INCOMING * 3)
    assert way_above["hub_boost"] == HUB_BOOST_MAX


def test_hub_boost_no_longer_saturates_at_old_threshold_of_five():
    """Regression guard for the actual bug fixed: incoming_count=5 used to saturate
    (old cap) — it must not saturate under the new, higher threshold."""
    result = _rerank_one(5)
    assert result["hub_boost"] < HUB_BOOST_MAX


def test_hub_boost_discriminates_across_measured_percentiles():
    """Post-repair vault distribution (mean 6.9, p75=9, p90=12, max=25) — the whole
    point of the recalibration is that these no longer collapse to the same value."""
    p75 = _rerank_one(9)["hub_boost"]
    p90 = _rerank_one(12)["hub_boost"]
    vault_max = _rerank_one(25)["hub_boost"]
    assert p75 < p90 <= vault_max
    assert p90 == HUB_BOOST_MAX
    assert vault_max == HUB_BOOST_MAX
