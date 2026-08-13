import numpy as np
import pytest

from app.discovery.embedder import EmbeddingCache, HashingEmbedder
from app.discovery.index import MetadataIndex
from app.discovery.metadata_loader import MetadataLoader
from app.discovery.search import HybridRetriever


@pytest.fixture()
def index(executor):
    objects, registry, glossary, fp = MetadataLoader(executor).load()
    return MetadataIndex(objects, registry, glossary, fp)


@pytest.fixture()
def retriever(index, tmp_path):
    return HybridRetriever(index, HashingEmbedder(), EmbeddingCache(tmp_path / "cache"))


def test_canonical_question_ranks_its_view_first(retriever):
    top = retriever.search("Who are the top 10 customers by lifetime revenue?", k=3)
    assert top[0].view_name == "vw_q002_top_10_customers_by_lifetime_revenue"
    assert top[0].canonical_question is not None
    assert top[0].category == "question_view"


def test_paraphrase_still_found(retriever):
    top = retriever.search("show me our biggest customers by total sales", k=3)
    assert "vw_q002_top_10_customers_by_lifetime_revenue" in [c.view_name for c in top]


def test_dead_stock_query(retriever):
    top = retriever.search("which items are dead stock", k=3)
    assert top[0].view_name == "vw_q011_items_dead_stock_or_severe_dead_stock"
    assert top[0].assumption_status == "DATA_SCIENCE_REVIEW_REQUIRED"


def test_keyword_only_mode_still_works(index):
    retriever = HybridRetriever(index, embedder=None, cache=None)
    assert retriever.mode == "keyword-only"
    top = retriever.search("top customers by revenue", k=3)
    assert "vw_q002_top_10_customers_by_lifetime_revenue" in [c.view_name for c in top]


def test_embedding_cache_roundtrip(index, tmp_path):
    cache = EmbeddingCache(tmp_path / "cache")

    class CountingEmbedder(HashingEmbedder):
        calls = 0

        def encode_corpus(self, texts):
            CountingEmbedder.calls += 1
            return super().encode_corpus(texts)

    HybridRetriever(index, CountingEmbedder(), cache)
    HybridRetriever(index, CountingEmbedder(), cache)  # second build → cache hit
    assert CountingEmbedder.calls == 1

    cached = cache.load(index.fingerprint, "hashing")
    assert isinstance(cached, np.ndarray)
