"""Embedding backends behind one interface.

- BgeEmbedder: production backend (BAAI/bge-base-en-v1.5 via sentence-transformers).
- HashingEmbedder: dependency-free deterministic backend for tests and for
  environments where the model cannot be downloaded.

If neither is usable the retriever degrades gracefully to keyword-only (BM25).
"""

import hashlib
import re
from pathlib import Path
from typing import Protocol

import numpy as np

from app.core.logging import get_logger

log = get_logger("embedder")

# bge models require this prefix on QUERIES only (not corpus documents).
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    backend: str

    def encode_corpus(self, texts: list[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...


class EmbedderUnavailable(RuntimeError):
    pass


class HashingEmbedder:
    """Deterministic bag-of-hashed-tokens vectors. No external model."""

    backend = "hashing"

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float32)
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.md5(token.encode()).digest()
            idx = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def encode_corpus(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vector(t) for t in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return self._vector(text)


class BgeEmbedder:
    backend = "bge"

    def __init__(self, model_name: str = "BAAI/bge-base-en-v1.5") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbedderUnavailable(
                "sentence-transformers not installed — pip install '.[ml]'"
            ) from exc
        try:
            self._model = SentenceTransformer(model_name)
        except Exception as exc:  # model download/load failure
            raise EmbedderUnavailable(f"could not load {model_name}: {exc}") from exc

    def encode_corpus(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        )

    def encode_query(self, text: str) -> np.ndarray:
        return np.asarray(
            self._model.encode(
                [BGE_QUERY_PREFIX + text],
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        )[0]


def build_embedder(backend: str, model_name: str) -> Embedder | None:
    """Returns an embedder, or None → retriever runs keyword-only."""
    if backend == "none":
        return None
    if backend == "hashing":
        return HashingEmbedder()
    try:
        return BgeEmbedder(model_name)
    except EmbedderUnavailable as exc:
        log.warning("embedder_unavailable_fallback_keyword_only", reason=str(exc))
        return None


class EmbeddingCache:
    """Corpus embeddings on disk, keyed by (schema fingerprint, backend)."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, fingerprint: str, backend: str) -> Path:
        return self._dir / f"emb_{backend}_{fingerprint}.npz"

    def load(self, fingerprint: str, backend: str) -> np.ndarray | None:
        path = self._path(fingerprint, backend)
        if not path.exists():
            return None
        try:
            return np.load(path)["embeddings"]
        except Exception:  # noqa: BLE001 - a corrupt/unreadable cache file must
            # only mean "recompute the embeddings", never crash startup.
            return None

    def save(self, fingerprint: str, backend: str, embeddings: np.ndarray) -> None:
        np.savez_compressed(self._path(fingerprint, backend), embeddings=embeddings)
