import json
import chromadb
from pathlib import Path

CONFIG_PATH = Path.home() / ".archiver-rag" / "config.json"

def _get_chroma_path() -> str:
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        return config["chroma_path"]
    # fallback for development before init is run
    return str(Path(__file__).parent.parent.parent / "persistence" / "chroma_db")

client = chromadb.PersistentClient(path=_get_chroma_path())
collection = client.get_or_create_collection(name="obsidian_vault", metadata={"hnsw:space":"cosine"})
