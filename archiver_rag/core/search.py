from archiver_rag.core.embedder import embed
from archiver_rag.core.db import collection
from archiver_rag.graph.rerank import rerank


def search_vault(
    query: str,
    n_results: int = 3,
    min_score: float = 0.35,
    context_note: str | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    query_vector = embed([query])[0]

    # Pre-filter by frontmatter type: field (stable taxonomy, unlike folder which drifts with auto_cluster)
    where: dict | None = {"type": {"$eq": type}} if type is not None else None

    # Fetch extra candidates when tag post-filtering will trim results
    fetch_n = n_results * 3 if tags else n_results

    query_kwargs: dict = {
        "query_embeddings": [query_vector],
        "n_results": fetch_n,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        query_kwargs["where"] = where

    results = collection.query(**query_kwargs)  # type: ignore[arg-type]

    documents = ((results.get("documents") or [[]])[0]) or []
    metadatas = ((results.get("metadatas") or [[]])[0]) or []
    distances = ((results.get("distances") or [[]])[0]) or []

    if not documents:
        return []

    reranked = rerank(
        docs=documents,
        metas=metadatas,
        dists=distances,
        query_note=context_note,
        min_score=min_score,
        n_results=fetch_n,
    )

    if tags:
        tag_set = {t.strip().lower() for t in tags}
        reranked = [
            r
            for r in reranked
            if tag_set
            & {t.strip().lower() for t in r.get("tags", "").split(",") if t.strip()}
        ]

    return reranked[:n_results]
