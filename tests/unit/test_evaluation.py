from app.discovery.embedder import HashingEmbedder
from app.discovery.evaluation import canonical_items, evaluate
from app.discovery.index import MetadataIndex
from app.discovery.metadata_loader import MetadataLoader
from app.discovery.search import HybridRetriever


def test_eval_on_fixture_registry(executor):
    objects, registry, glossary, fp = MetadataLoader(executor).load()
    index = MetadataIndex(objects, registry, glossary, fp)
    retriever = HybridRetriever(index, HashingEmbedder())

    report = evaluate(retriever, canonical_items(registry), set(objects))

    # q005's view doesn't exist in the fixture → must be skipped, not failed
    assert report.skipped_missing_view == 1
    assert report.total == 2
    assert report.top1_accuracy == 1.0
    assert report.top3_accuracy == 1.0
    assert report.misses == []
