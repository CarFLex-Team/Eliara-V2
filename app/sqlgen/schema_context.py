"""Builds the minimal schema slice Haiku is allowed to see."""

from pydantic import BaseModel

from app.core.models import ViewCandidate
from app.discovery.index import MetadataIndex

_MAX_OBJECTS = 15
_MAX_COLUMNS_PER_OBJECT = 60


class TableSlice(BaseModel):
    name: str
    kind: str
    columns: list[str]
    # column -> one real value from the object. Without this the generator has
    # only column NAMES and must guess value formats; a `year_month` guessed as
    # "202604" when the data holds "2026-04" returns zero rows, which then reads
    # as "no such data" instead of "the filter never matched". Empty when the
    # object had no rows at discovery time.
    samples: dict[str, str] = {}


def build_slice(
    index: MetadataIndex,
    requested_tables: list[str],
    candidates: list[ViewCandidate],
    max_objects: int = _MAX_OBJECTS,
) -> list[TableSlice]:
    """Requested objects first (validated against the index), then retrieval
    candidates as context, capped hard. Unknown names are silently dropped —
    Haiku never learns about objects outside the analytics surface."""
    ordered: list[str] = []
    for name in requested_tables:
        if name in index.objects and name not in ordered:
            ordered.append(name)
    for candidate in candidates:
        if candidate.view_name in index.objects and candidate.view_name not in ordered:
            ordered.append(candidate.view_name)
    ordered = ordered[:max_objects]

    return [
        TableSlice(
            name=name,
            kind=index.objects[name].kind,
            columns=index.objects[name].columns[:_MAX_COLUMNS_PER_OBJECT],
            samples={
                column: value
                for column, value in index.objects[name].samples.items()
                if column in set(index.objects[name].columns[:_MAX_COLUMNS_PER_OBJECT])
            },
        )
        for name in ordered
    ]
