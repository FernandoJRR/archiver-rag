import json
import chromadb
from pathlib import Path

CONFIG_PATH = Path.home() / ".archiver-rag" / "config.json"


def _get_chroma_path() -> str:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "archiver-rag is not configured. Run 'archiver-rag init' first."
        )
    return json.loads(CONFIG_PATH.read_text())["chroma_path"]


class _LazyCollection:
    """Proxy that defers ChromaDB initialization to first use.

    Keeps the MCP server importable even before 'archiver-rag init' has run,
    so tool errors reach the agent instead of crashing the server at startup.
    """

    def __init__(self) -> None:
        self._collection: chromadb.Collection | None = None

    def _get(self) -> chromadb.Collection:
        if self._collection is None:
            client = chromadb.PersistentClient(path=_get_chroma_path())
            self._collection = client.get_or_create_collection(
                name="obsidian_vault",
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


collection = _LazyCollection()
