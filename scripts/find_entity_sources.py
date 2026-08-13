#!/usr/bin/env python3
"""Find where customer/supplier names actually live in this database.

The entity index auto-detects `{entity}_code` + `{entity}_name` column pairs in
dimension tables. When an entity is missing from `entity_index_built`, the data
is somewhere else — a fact table, a view, or under different column names.

    python scripts/find_entity_sources.py [path/to/eliara_master.db]

Prints every object carrying a *_name column, and suggests the setting to add.
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

INTERESTING = ("customer", "supplier", "vendor", "card", "business_partner", "bp")


def main() -> int:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eliara_master.db")
    if not db.exists():
        print(f"Database not found: {db}")
        print("Usage: python scripts/find_entity_sources.py path/to/eliara_master.db")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    objects = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()

    pairs = defaultdict(list)   # entity -> [(object, type, has_code)]
    for name, kind in objects:
        try:
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
        except sqlite3.Error:
            continue
        for column in columns:
            if not column.lower().endswith("_name"):
                continue
            entity = column[: -len("_name")]
            pairs[entity].append((name, kind, f"{entity}_code" in columns))

    print(f"{db}\n")
    print("=" * 72)
    print("ENTITIES FOUND (any object with a *_name column)")
    print("=" * 72)
    for entity in sorted(pairs):
        marker = "  <<<" if any(k in entity.lower() for k in INTERESTING) else ""
        print(f"\n{entity}{marker}")
        for obj, kind, has_code in sorted(pairs[entity])[:8]:
            code = "code+name" if has_code else "name only"
            print(f"    {kind:<5} {obj:<52} {code}")
        if len(pairs[entity]) > 8:
            print(f"    ... and {len(pairs[entity]) - 8} more")

    print("\n" + "=" * 72)
    print("SUGGESTED SETTING")
    print("=" * 72)
    suggestions = []
    for entity in sorted(pairs):
        if not any(k in entity.lower() for k in INTERESTING):
            continue
        # Prefer a dim table, then any table, then a view; require code+name.
        ranked = sorted(
            pairs[entity],
            key=lambda t: (
                not t[2],
                not t[0].startswith("dim"),
                t[1] != "table",
                len(t[0]),
            ),
        )
        if ranked:
            suggestions.append(f"{entity}={ranked[0][0]}")

    if suggestions:
        print("\nAdd to .env:\n")
        print(f'  ELIARA_ENTITY_INDEX_SOURCES={",".join(suggestions)}\n')
        print("Then rebuild. Watch for 'entity_override_applied' in the logs.")
    else:
        print("\nNo customer/supplier-like columns found at all.")
        print("Check what the customer name column is actually called above.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
