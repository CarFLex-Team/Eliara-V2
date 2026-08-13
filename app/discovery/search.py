"""Hybrid view retrieval: BM25 keyword + embedding cosine, fused with RRF."""

import re

import numpy as np
from rank_bm25 import BM25Okapi

from app.core.logging import get_logger
from app.core.models import ViewCandidate
from app.discovery.embedder import Embedder, EmbeddingCache
from app.discovery.index import MetadataIndex

log = get_logger("retriever")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RRF_K = 60
_QUESTION_VIEW_BOOST = 0.004  # tie-breaker in favor of purpose-built vw_q* views


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class HybridRetriever:
    def __init__(
        self,
        index: MetadataIndex,
        embedder: Embedder | None = None,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._docs = index.searchable_documents()
        self._names = [d.name for d in self._docs]
        self._bm25 = BM25Okapi([_tokenize(d.text) for d in self._docs])

        self._embeddings: np.ndarray | None = None
        if embedder is not None:
            cached = cache.load(index.fingerprint, embedder.backend) if cache else None
            if cached is not None and len(cached) == len(self._docs):
                self._embeddings = cached
                log.info("embeddings_cache_hit", fingerprint=index.fingerprint)
            else:
                self._embeddings = embedder.encode_corpus([d.text for d in self._docs])
                if cache:
                    cache.save(index.fingerprint, embedder.backend, self._embeddings)
                log.info("embeddings_built", docs=len(self._docs), backend=embedder.backend)

    @property
    def mode(self) -> str:
        if self._embeddings is None:
            return "keyword-only"
        return f"hybrid/{self._embedder.backend}"

    def search(self, query: str, k: int = 8) -> list[ViewCandidate]:
        fused: dict[int, float] = {}

        bm25_scores = self._bm25.get_scores(_tokenize(query))
        for rank, doc_idx in enumerate(np.argsort(bm25_scores)[::-1][: max(k * 4, 20)]):
            if bm25_scores[doc_idx] > 0:
                fused[int(doc_idx)] = fused.get(int(doc_idx), 0.0) + 1.0 / (_RRF_K + rank + 1)

        if self._embeddings is not None:
            query_vec = self._embedder.encode_query(query)
            cosine = self._embeddings @ query_vec
            for rank, doc_idx in enumerate(np.argsort(cosine)[::-1][: max(k * 4, 20)]):
                fused[int(doc_idx)] = fused.get(int(doc_idx), 0.0) + 1.0 / (_RRF_K + rank + 1)

        for doc_idx in fused:
            if self._index.objects[self._names[doc_idx]].category == "question_view":
                fused[doc_idx] += _QUESTION_VIEW_BOOST

        top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [self._index.candidate(self._names[i], score) for i, score in top]
