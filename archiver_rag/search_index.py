import sys
from archiver_rag.core.embedder import embed
from archiver_rag.core.db import collection

query = sys.argv[1]
query_vector = embed([query])[0]

results = collection.query(
    query_embeddings=[query_vector],
    n_results=3,
    include=["documents", "metadatas", "distances"]
)

docs = results["documents"][0]
if not docs:
    print("No results — index may be empty")
else:
    for doc, meta, dist in zip(docs, results["metadatas"][0], results["distances"][0]):
        print(f"Source: {meta['source']}")
        print(f"Score: {round(1 - dist, 3)}")
        print(f"Preview: {doc[:200]}")
        print("---")
