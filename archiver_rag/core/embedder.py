import os
from pathlib import Path

MODEL_NAME = "all-MiniLM-L6-v2"
_model = None


def _is_cached() -> bool:
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    # HuggingFace stores models as "models--org--modelname"
    model_dir = cache_dir / f"models--sentence-transformers--{MODEL_NAME}"
    return model_dir.exists()


if _is_cached():
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _get_model():
    global _model
    if _model is None:
        if _is_cached():
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(texts: list[str]) -> list:
    return _get_model().encode(texts, show_progress_bar=False).tolist()
