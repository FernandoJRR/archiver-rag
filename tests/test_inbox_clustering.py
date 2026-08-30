"""Tests for graph/inbox.py — Gate 2 inbox clustering by embedding similarity.

Uses the tmp_vault fixture (disk-backed) with core.embedder.embed monkeypatched to
deterministic vectors, same pattern as tests/test_placement.py. prune_orphans is
patched out to avoid real ChromaDB calls, same pattern as tests/test_delete_note.py —
move_notes (called by maybe_spin_out_clusters) triggers it internally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archiver_rag.graph.inbox import (
    group_inbox_notes,
    name_cluster,
    _resolve_cluster_folder,
    maybe_spin_out_clusters,
)
from archiver_rag.vault.folder_notes import read_folder_note


@pytest.fixture(autouse=True)
def _no_prune(monkeypatch):
    """Block real ChromaDB calls — move_notes imports prune_orphans lazily inside its
    function body, so patching the source module is enough."""
    monkeypatch.setattr(
        "archiver_rag.core.ingest.prune_orphans", lambda *_a, **_kw: 0, raising=False
    )


def _fake_embed_by_group(texts: list[str]) -> list[list[float]]:
    """Two orthogonal directions keyed by a 'groupa'/'groupb' marker anywhere in the
    text (tags or body) — same technique test_placement.py uses for scores it wants to
    control precisely, generalized to distinguish more than one candidate direction."""
    vecs = []
    for t in texts:
        low = t.lower()
        if "groupa" in low:
            vecs.append([1.0, 0.0])
        elif "groupb" in low:
            vecs.append([0.0, 1.0])
        else:
            vecs.append([0.5, 0.5])
    return vecs


# ──────────────────────────────────────────────────────────────────────────────
# group_inbox_notes
# ──────────────────────────────────────────────────────────────────────────────

def test_group_inbox_notes_splits_by_similarity(tmp_vault, monkeypatch):
    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed_by_group)

    a1 = tmp_vault.write(
        "inbox/alpha-one.md", '---\ntags: ["groupa"]\n---\nAbout groupa things.'
    )
    a2 = tmp_vault.write(
        "inbox/alpha-two.md", '---\ntags: ["groupa"]\n---\nMore groupa things.'
    )
    b1 = tmp_vault.write(
        "inbox/beta-one.md", '---\ntags: ["groupb"]\n---\nAbout groupb things.'
    )

    groups = group_inbox_notes(tmp_vault.root, [a1, a2, b1], threshold=0.9)
    group_sets = [frozenset(g) for g in groups]

    assert frozenset({a1, a2}) in group_sets
    assert frozenset({b1}) in group_sets


def test_group_inbox_notes_singletons_when_nothing_similar(tmp_vault, monkeypatch):
    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed_by_group)

    a1 = tmp_vault.write(
        "inbox/alpha-one.md", '---\ntags: ["groupa"]\n---\nAbout groupa things.'
    )
    b1 = tmp_vault.write(
        "inbox/beta-one.md", '---\ntags: ["groupb"]\n---\nAbout groupb things.'
    )

    groups = group_inbox_notes(tmp_vault.root, [a1, b1], threshold=0.9)
    assert sorted(len(g) for g in groups) == [1, 1]


def test_group_inbox_notes_empty_input_returns_empty(tmp_vault):
    assert group_inbox_notes(tmp_vault.root, [], threshold=0.5) == []


# ──────────────────────────────────────────────────────────────────────────────
# name_cluster
# ──────────────────────────────────────────────────────────────────────────────

def test_name_cluster_derives_terms_from_shared_tags(tmp_vault):
    a = tmp_vault.write(
        "inbox/a.md", '---\ntags: ["weekly-recipes", "cooking"]\n---\nBody.'
    )
    b = tmp_vault.write("inbox/b.md", '---\ntags: ["weekly-recipes"]\n---\nBody.')
    c = tmp_vault.write(
        "inbox/c.md", '---\ntags: ["weekly-recipes", "baking"]\n---\nBody.'
    )

    desc, _dist = name_cluster([a, b, c])
    assert "weekly-recipes" in desc


def test_name_cluster_no_tags_returns_empty(tmp_vault):
    a = tmp_vault.write("inbox/a.md", "# A\nNo tags here.")
    desc, dist = name_cluster([a])
    assert desc == []
    assert dist == []


# ──────────────────────────────────────────────────────────────────────────────
# _resolve_cluster_folder — collision safety
# ──────────────────────────────────────────────────────────────────────────────

def test_resolve_cluster_folder_no_collision(tmp_vault):
    assert _resolve_cluster_folder(tmp_vault.root, "weekly-recipes") == "weekly-recipes"


def test_resolve_cluster_folder_collision_appends_suffix(tmp_vault):
    (tmp_vault.root / "weekly-recipes").mkdir()
    assert _resolve_cluster_folder(tmp_vault.root, "weekly-recipes") == "weekly-recipes-1"


def test_resolve_cluster_folder_empty_slug_falls_back(tmp_vault):
    assert _resolve_cluster_folder(tmp_vault.root, "") == "inbox-cluster"


# ──────────────────────────────────────────────────────────────────────────────
# maybe_spin_out_clusters — end to end
# ──────────────────────────────────────────────────────────────────────────────

def test_spins_out_qualifying_cluster_into_new_described_folder(tmp_vault, monkeypatch):
    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed_by_group)

    a1 = tmp_vault.write(
        "inbox/alpha-one.md", '---\ntags: ["groupa"]\n---\nAbout groupa things.'
    )
    a2 = tmp_vault.write(
        "inbox/alpha-two.md", '---\ntags: ["groupa"]\n---\nMore groupa things.'
    )
    a3 = tmp_vault.write(
        "inbox/alpha-three.md", '---\ntags: ["groupa"]\n---\nEven more groupa things.'
    )

    spun_out = maybe_spin_out_clusters(
        tmp_vault.root, min_cluster_size=3, threshold=0.9
    )

    assert len(spun_out) == 1
    new_folder = spun_out[0]["folder"]
    assert new_folder != "inbox"
    # "notes" must be the PRE-move, inbox-relative paths — watcher.py's
    # _maybe_cluster only knows the note it just placed into inbox/ by that path,
    # and checks membership against it to decide whether to report the new folder
    # instead of "inbox" as the note's final resting place.
    assert set(spun_out[0]["notes"]) == {
        "inbox/alpha-one.md",
        "inbox/alpha-two.md",
        "inbox/alpha-three.md",
    }
    assert not (tmp_vault.root / "inbox" / "alpha-one.md").exists()
    assert (tmp_vault.root / new_folder / "alpha-one.md").exists()
    assert (tmp_vault.root / new_folder / "alpha-two.md").exists()
    assert (tmp_vault.root / new_folder / "alpha-three.md").exists()

    note = read_folder_note(tmp_vault.root, new_folder)
    assert note is not None
    assert note.source == "auto"
    assert "groupa" in note.description_terms


def test_group_below_min_size_stays_in_inbox(tmp_vault, monkeypatch):
    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed_by_group)

    a1 = tmp_vault.write(
        "inbox/alpha-one.md", '---\ntags: ["groupa"]\n---\nAbout groupa things.'
    )
    a2 = tmp_vault.write(
        "inbox/alpha-two.md", '---\ntags: ["groupa"]\n---\nMore groupa things.'
    )
    b1 = tmp_vault.write(
        "inbox/beta-one.md", '---\ntags: ["groupb"]\n---\nAbout groupb things.'
    )

    spun_out = maybe_spin_out_clusters(
        tmp_vault.root, min_cluster_size=3, threshold=0.9
    )

    assert spun_out == []
    assert (tmp_vault.root / "inbox" / "alpha-one.md").exists()
    assert (tmp_vault.root / "inbox" / "alpha-two.md").exists()
    assert (tmp_vault.root / "inbox" / "beta-one.md").exists()


def test_fewer_than_min_cluster_size_notes_total_short_circuits(tmp_vault, monkeypatch):
    """No embedding work should happen at all when the inbox doesn't even have
    min_cluster_size notes yet."""
    calls = []
    monkeypatch.setattr(
        "archiver_rag.core.embedder.embed", lambda texts: calls.append(texts) or []
    )
    tmp_vault.write("inbox/only-one.md", '---\ntags: ["groupa"]\n---\nBody.')

    spun_out = maybe_spin_out_clusters(tmp_vault.root, min_cluster_size=3, threshold=0.9)

    assert spun_out == []
    assert calls == []


def test_missing_inbox_directory_returns_empty(tmp_vault):
    assert maybe_spin_out_clusters(tmp_vault.root, min_cluster_size=3) == []


def test_collision_with_existing_folder_gets_numeric_suffix(tmp_vault, monkeypatch):
    monkeypatch.setattr("archiver_rag.core.embedder.embed", _fake_embed_by_group)
    (tmp_vault.root / "groupa").mkdir()

    a1 = tmp_vault.write(
        "inbox/alpha-one.md", '---\ntags: ["groupa"]\n---\nAbout groupa things.'
    )
    a2 = tmp_vault.write(
        "inbox/alpha-two.md", '---\ntags: ["groupa"]\n---\nMore groupa things.'
    )
    a3 = tmp_vault.write(
        "inbox/alpha-three.md", '---\ntags: ["groupa"]\n---\nEven more groupa things.'
    )

    spun_out = maybe_spin_out_clusters(
        tmp_vault.root, min_cluster_size=3, threshold=0.9
    )

    assert len(spun_out) == 1
    assert spun_out[0]["folder"] != "groupa"
    assert spun_out[0]["folder"].startswith("groupa-")
