from app.discovery.index import MetadataIndex
from app.discovery.metadata_loader import MetadataLoader
from app.sqlgen.schema_context import build_slice


def _index(executor) -> MetadataIndex:
    objects, registry, glossary, fp = MetadataLoader(executor).load()
    return MetadataIndex(objects, registry, glossary, fp)


def test_requested_first_then_candidates(executor):
    index = _index(executor)
    candidates = [index.candidate("dim_b3_item", 0.5)]
    slice_ = build_slice(index, ["fact_ai_sales_net"], candidates)
    names = [t.name for t in slice_]
    assert names[0] == "fact_ai_sales_net"
    assert "dim_b3_item" in names


def test_unknown_and_forbidden_names_silently_dropped(executor):
    index = _index(executor)
    slice_ = build_slice(
        index,
        ["sap_oitm_raw", "batch_09_import_evidence", "no_such_table", "dim_b3_item"],
        [],
    )
    assert [t.name for t in slice_] == ["dim_b3_item"]


def test_hard_cap(executor):
    index = _index(executor)
    all_names = list(index.objects)
    slice_ = build_slice(index, all_names, [], max_objects=3)
    assert len(slice_) == 3
