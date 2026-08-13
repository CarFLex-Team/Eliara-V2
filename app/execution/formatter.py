"""QueryResult -> compact markdown payload for the answer prompt."""

from app.core.models import QueryResult

_MAX_CHARS_DEFAULT = 8000


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    return str(value)


def to_llm_payload(result: QueryResult, max_chars: int = _MAX_CHARS_DEFAULT) -> tuple[str, int]:
    """Returns (markdown_table, rows_shown). Trims rows to fit the char budget."""
    if not result.rows:
        return "(empty result set)", 0

    header = "| " + " | ".join(result.columns) + " |"
    divider = "| " + " | ".join("---" for _ in result.columns) + " |"
    lines = [header, divider]
    shown = 0
    used = len(header) + len(divider) + 2
    for row in result.rows:
        line = "| " + " | ".join(_format_cell(v) for v in row) + " |"
        if used + len(line) > max_chars and shown > 0:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < result.row_count:
        lines.append(f"... ({result.row_count - shown} more rows omitted)")
    return "\n".join(lines), shown
