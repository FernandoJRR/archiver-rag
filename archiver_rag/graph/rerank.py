from pathlib import Path
from archiver_rag.utils import get_vault_path, build_link_map

# hub_boost saturates at HUB_BOOST_SATURATION_INCOMING inbound links, capped at
# HUB_BOOST_MAX. Originally saturated at 5 — calibrated when auto_link's per-run
# append-only cap had driven mean inbound links to 25.7 (90% of notes >= 5), which
# made this a near-constant +0.10 for nearly every result. After the wikilink
# desaturation fix (see AGENTS.md "Desaturating the wikilink graph" / vault note
# wikilink-graph-desaturated-margin-based-linking-replaces-per-run-cap),
# post-repair inbound counts measured mean 6.9, p75=9, p90=12, max=25 — 12 (~p90)
# keeps the boost discriminative for the genuine hubs instead of maxing out ~69%
# of results the way 5 did against the new distribution.
HUB_BOOST_SATURATION_INCOMING = 12
HUB_BOOST_MAX = 0.10


def rerank(
    docs: list,
    metas: list,
    dists: list,
    vault_path: str | None = None,
    query_note: str | None = None,
    min_score: float = 0.35,
    n_results: int = 5,
) -> list[dict]:
    resolved_vault = vault_path or get_vault_path()
    outgoing_map, incoming_map = build_link_map(Path(resolved_vault))
    query_stem = Path(query_note).stem if query_note else None

    reranked: list[dict] = []

    for doc, meta, dist in zip(docs, metas, dists):
        source_stem = Path(str(meta["source"])).stem
        base_score = round(1 - (dist / 2), 3)

        if base_score < min_score:
            continue

        graph_boost = 0.0
        if query_stem:
            if source_stem in outgoing_map.get(query_stem, []):
                graph_boost += 0.10
            if source_stem in incoming_map.get(query_stem, []):
                graph_boost += 0.10

        incoming_count = int(meta.get("incoming_count", 0))
        hub_boost = round(
            min(
                incoming_count * (HUB_BOOST_MAX / HUB_BOOST_SATURATION_INCOMING),
                HUB_BOOST_MAX,
            ),
            3,
        )

        final_score = round(base_score + graph_boost + hub_boost, 3)

        reranked.append(
            {
                "content": doc,
                "source": str(meta["source"]),
                "folder": str(meta.get("folder", "")),
                "type": str(meta.get("type", "")),
                "tags": str(meta.get("tags", "")),
                "title": str(meta.get("title", "")),
                "relevance_score": final_score,
                "base_score": base_score,
                "graph_boost": round(graph_boost, 3),
                "hub_boost": hub_boost,
            }
        )

    reranked.sort(key=lambda x: x["relevance_score"], reverse=True)
    return reranked[:n_results]
