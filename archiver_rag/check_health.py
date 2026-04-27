from archiver_rag.core.db import collection

count = collection.count()
print(f"Total chunks in index: {count}")

if count > 0:
    # Peek at first few chunks
    results = collection.peek(limit=3)
    for i, (doc, meta) in enumerate(zip(results["documents"], results["metadatas"])):
        print(f"\n--- Chunk {i+1} ---")
        print(f"Source: {meta['source']}")
        print(f"Preview: {doc[:200]}")
else:
    print("Index is empty — ingest hasn't run or failed silently")
