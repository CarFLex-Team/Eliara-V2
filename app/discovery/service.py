"""Composition root for the discovery layer (startup + DB-refresh rebuild)."""

from app.core.config import Settings
from app.discovery.embedder import EmbeddingCache, build_embedder
from app.discovery.index import MetadataIndex
from app.discovery.metadata_loader import MetadataLoader
from app.discovery.search import HybridRetriever


def build_discovery(executor, settings: Settings, registry_table: str = "chatbot_question_view_registry") -> tuple[MetadataIndex, HybridRetriever]:
    objects, registry, glossary, fingerprint = MetadataLoader(executor, registry_table).load()
    index = MetadataIndex(objects, registry, glossary, fingerprint)
    embedder = build_embedder(settings.embedding_backend, settings.embedding_model_name)
    cache = EmbeddingCache(settings.embedding_cache_dir)
    retriever = HybridRetriever(index, embedder, cache)
    return index, retriever
