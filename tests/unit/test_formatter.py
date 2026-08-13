from app.core.models import QueryResult
from app.execution.formatter import to_llm_payload


def _result(rows, columns=("a", "b"), truncated=False):
    return QueryResult(
        columns=list(columns), rows=rows, row_count=len(rows),
        truncated=truncated, source="view", object_name="v", elapsed_ms=1,
    )


def test_markdown_shape_and_number_formatting():
    payload, shown = to_llm_payload(_result([("X", 1234567.891), ("Y", 5.0)]))
    assert payload.splitlines()[0] == "| a | b |"
    assert "1,234,567.89" in payload
    assert "| Y | 5 |" in payload
    assert shown == 2


def test_empty_result():
    payload, shown = to_llm_payload(_result([]))
    assert payload == "(empty result set)"
    assert shown == 0


def test_char_budget_trims_rows():
    rows = [(f"row{i}", "x" * 50) for i in range(200)]
    payload, shown = to_llm_payload(_result(rows), max_chars=1000)
    assert shown < 200
    assert "more rows omitted" in payload


def test_none_rendered_empty():
    payload, _ = to_llm_payload(_result([("X", None)]))
    assert "| X |  |" in payload
