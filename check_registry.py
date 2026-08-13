import sqlite3, sys

db_path = sys.argv[1] if len(sys.argv) > 1 else r"data\companies\beta\beta.db"
conn = sqlite3.connect(db_path)
names = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
).fetchall()]

targets = {
    "chatbot_question_view_registry": "registry (curated Q&A -> view mapping)",
    "vw_gold_business_glossary": "glossary (option 1)",
    "batch_13_business_glossary": "glossary (option 2)",
}

print(f"Total tables/views in {db_path}: {len(names)}\n")
for t, desc in targets.items():
    found = t in names
    print(f"  [{'FOUND' if found else 'MISSING'}] {t}  ({desc})")

print("\nAll table/view names containing 'registry' or 'glossary':")
matches = [n for n in names if "registry" in n.lower() or "glossary" in n.lower()]
if matches:
    for m in matches:
        print(f"  - {m}")
else:
    print("  (none)")