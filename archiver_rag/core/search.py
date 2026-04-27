from archiver_rag.core.embedder import embed
from archiver_rag.core.db import collection
from archiver_rag.graph.rerank import rerank


def search_vault(
    query: str,
    n_results: int = 3,
    min_score: float = 0.35,
    context_note: str | None = None,
) -> list[dict]:
    query_vector = embed([query])[0]

    results = collection.query(
        query_embeddings=[query_vector],
        n_results=n_results,
        include=["documents", "metadatas", "distances"]
    )

    documents = ((results.get("documents") or [[]])[0]) or []
    metadatas = ((results.get("metadatas") or [[]])[0]) or []
    distances = ((results.get("distances") or [[]])[0]) or []

    if not documents:
        return []

    return rerank(
        docs=documents,
        metas=metadatas,
        dists=distances,
        query_note=context_note,
        min_score=min_score,
        n_results=n_results,
    )
