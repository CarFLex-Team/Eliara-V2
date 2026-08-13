"""AST validation gate for generated SQL — nothing executes without passing it.

sqlglot parses the statement; we then enforce, in order:
  1. exactly one statement
  2. the root is a plain query (SELECT / UNION / CTE+SELECT)
  3. no forbidden constructs anywhere in the tree
  4. no dangerous functions
  5. every referenced table exists in the whitelist (CTE names excluded)
  6. every referenced column exists in the referenced tables (aliases excluded)
  7. LIMIT present and <= max_rows (injected/reduced otherwise)
The SQL that runs is REGENERATED from the validated AST, never the raw text.
"""

import sqlglot
from pydantic import BaseModel
from sqlglot import expressions as exp

from app.core.errors import SQLValidationError

_FORBIDDEN_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command",
        "Pragma", "Attach", "Detach", "TruncateTable", "Merge", "Grant",
        "AlterTable", "Analyze",
    )
    if hasattr(exp, name)
)
_FORBIDDEN_FUNCTIONS = {
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "zipfile",
}


class ValidatedSQL(BaseModel):
    sql: str
    tables: list[str]


def validate_sql(raw_sql: str, whitelist: dict[str, set[str]], max_rows: int) -> ValidatedSQL:
    try:
        statements = sqlglot.parse(raw_sql, read="sqlite")
    except sqlglot.errors.SqlglotError as exc:
        # SqlglotError covers ParseError AND TokenError (e.g. prose around the
        # SQL, unterminated strings). Everything funnels into the same
        # rejection path so the corrective retry can fire.
        raise SQLValidationError(internal_detail=f"unparseable SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLValidationError(internal_detail=f"{len(statements)} statements, expected 1")
    root = statements[0]

    if not isinstance(root, (exp.Select, exp.Union)):
        raise SQLValidationError(internal_detail=f"root is {type(root).__name__}, not a query")

    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLValidationError(
                internal_detail=f"forbidden construct: {type(node).__name__}"
            )
        if isinstance(node, exp.Func):
            name = (node.sql_name() or "").lower()
            anon = node.name.lower() if isinstance(node, exp.Anonymous) else ""
            if name in _FORBIDDEN_FUNCTIONS or anon in _FORBIDDEN_FUNCTIONS:
                raise SQLValidationError(internal_detail=f"forbidden function: {name or anon}")
        if isinstance(node, exp.Join):
            side = (node.args.get("side") or "").upper()
            # FULL and RIGHT outer joins depend on the SQLite build (FULL only
            # since 3.39, Nov 2021) and aren't guaranteed at the deploy target
            # even when they work in dev — caught here, before the executor,
            # with a reason specific enough for the corrective retry to act on.
            if side in {"FULL", "RIGHT"}:
                raise SQLValidationError(
                    internal_detail=(
                        f"{side} OUTER JOIN is not portable across SQLite builds — "
                        "rewrite using LEFT JOIN (swap table order for RIGHT; for "
                        "FULL, LEFT JOIN each direction and UNION them)"
                    )
                )

    lower_whitelist = {name.lower(): cols for name, cols in whitelist.items()}
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    derived_aliases = {
        sub.alias_or_name.lower()
        for sub in root.find_all(exp.Subquery)
        if sub.alias_or_name
    }

    referenced: list[str] = []
    for table in root.find_all(exp.Table):
        name = table.name.lower()
        if name in cte_names or name in derived_aliases:
            continue
        if name not in lower_whitelist:
            raise SQLValidationError(internal_detail=f"table not in whitelist: {table.name}")
        if name not in referenced:
            referenced.append(name)

    if not referenced:
        raise SQLValidationError(
            internal_detail="query references no whitelisted tables (no-op query)"
        )

    allowed_columns = {
        col.lower() for name in referenced for col in lower_whitelist[name]
    }
    output_aliases = {
        a.alias.lower() for a in root.find_all(exp.Alias) if a.alias
    } | cte_names
    for column in root.find_all(exp.Column):
        col_name = column.name.lower()
        if col_name == "*" or not col_name:
            continue
        if col_name not in allowed_columns and col_name not in output_aliases:
            raise SQLValidationError(internal_detail=f"unknown column: {column.name}")

    limit_node = root.args.get("limit")
    if limit_node is None:
        root = root.limit(max_rows)
    else:
        try:
            current = int(limit_node.expression.this)
        except (TypeError, ValueError, AttributeError):
            current = max_rows + 1
        if current > max_rows:
            root = root.limit(max_rows)

    return ValidatedSQL(sql=root.sql(dialect="sqlite"), tables=referenced)
