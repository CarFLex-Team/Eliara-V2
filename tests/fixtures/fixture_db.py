"""Builds a miniature Eliara database for tests.

Table DDL is copied VERBATIM from the production schema report
(fact_ai_sales_net, dim_b3_item, dim_b3_item_group, dim_b3_warehouse,
chatbot_question_view_registry). The two vw_q* views carry the real
production names but simplified stand-in bodies — view business logic is not
under test until M2+; M1 tests target executor security and behavior.
"""

import sqlite3
from pathlib import Path

DDL = [
    '''CREATE TABLE "fact_ai_sales_net" (
"source_document" TEXT,
  "posting_date" TEXT, "posting_date_iso" TEXT,
  "document_date" TEXT, "document_date_iso" TEXT,
  "due_date" TEXT, "due_date_iso" TEXT,
  "year" TEXT, "year_month" TEXT,
  "customer_code" TEXT, "customer_name" TEXT,
  "document_number" TEXT, "document_key" TEXT, "line_number" REAL,
  "item_code" TEXT, "item_name" TEXT, "item_group_name" TEXT, "item_description" TEXT,
  "warehouse_code" TEXT, "warehouse_name" TEXT,
  "sales_employee_code" TEXT, "sales_employee_name" TEXT,
  "net_quantity" REAL, "net_revenue" REAL, "net_gross_profit" REAL, "net_tax_amount" REAL
)''',
    '''CREATE TABLE dim_b3_item(
  item_code TEXT, item_name TEXT, foreign_name TEXT, item_group_code TEXT,
  inventory_uom TEXT, sales_uom TEXT, purchase_uom TEXT,
  total_on_hand REAL, total_committed REAL, total_on_order REAL,
  average_price REAL, last_purchase_price REAL, last_purchase_date TEXT,
  create_date TEXT, update_date TEXT, valid_for TEXT, frozen_for TEXT,
  sales_item TEXT, purchase_item TEXT, inventory_item TEXT
)''',
    "CREATE TABLE dim_b3_item_group(item_group_code TEXT, item_group_name TEXT)",
    '''CREATE TABLE dim_b3_warehouse(
  warehouse_code TEXT, warehouse_name TEXT, location TEXT,
  street TEXT, city TEXT, country TEXT, inactive TEXT
)''',
    '''CREATE TABLE chatbot_question_view_registry (
        question_id INTEGER PRIMARY KEY,
        canonical_question TEXT NOT NULL,
        view_name TEXT NOT NULL UNIQUE,
        formula_version TEXT,
        assumption_status TEXT,
        time_scope_rule TEXT,
        requires_endpoint_filter INTEGER NOT NULL DEFAULT 0,
        implementation_note TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    "CREATE TABLE batch_13_business_glossary(term TEXT, definition TEXT)",
    "CREATE TABLE batch_09_import_evidence(x TEXT)",
    "CREATE TABLE sap_oitm_raw(item TEXT)",
    '''CREATE VIEW vw_q011_items_dead_stock_or_severe_dead_stock AS
       SELECT item_code, item_name, 0 AS months_without_sales FROM dim_b3_item''',
    '''CREATE VIEW vw_q002_top_10_customers_by_lifetime_revenue AS
       SELECT customer_code, customer_name,
              SUM(net_revenue) AS lifetime_revenue,
              SUM(net_gross_profit) AS lifetime_gross_profit
       FROM fact_ai_sales_net
       GROUP BY customer_code, customer_name
       ORDER BY lifetime_revenue DESC
       LIMIT 10''',
    '''CREATE VIEW vw_ai_sales_by_year AS
       SELECT year, SUM(net_revenue) AS net_revenue, SUM(net_quantity) AS net_quantity
       FROM fact_ai_sales_net GROUP BY year''',
]

SEED_SALES = [
    ("AR Invoice", "01/10/2020", "2020-10-01", "01/10/2020", "2020-10-01", "31/10/2020",
     "2020-10-31", "2020", "2020-10", "C001", "Alpha Trading", "INV-1", "K1", 1.0,
     "A100", "Brake Pad", "Brakes", "Front brake pad", "WH1", "Main WH",
     "S01", "Ali", 10.0, 5000.0, 1500.0, 250.0),
    ("AR Invoice", "15/03/2024", "2024-03-15", "15/03/2024", "2024-03-15", "14/04/2024",
     "2024-04-14", "2024", "2024-03", "C002", "Beta Motors", "INV-2", "K2", 1.0,
     "A200", "Oil Filter", "Filters", "Oil filter std", "WH1", "Main WH",
     "S01", "Ali", 40.0, 8000.0, 3200.0, 400.0),
    ("AR Invoice", "27/06/2026", "2026-06-27", "27/06/2026", "2026-06-27", "27/07/2026",
     "2026-07-27", "2026", "2026-06", "C001", "Alpha Trading", "INV-3", "K3", 1.0,
     "A100", "Brake Pad", "Brakes", "Front brake pad", "WH2", "Branch WH",
     "S02", "Sara", 5.0, 2600.0, 900.0, 130.0),
]

SEED_REGISTRY = [
    (2, "Who are the top 10 customers by lifetime revenue?",
     "vw_q002_top_10_customers_by_lifetime_revenue", "v1", "APPROVED_LOGIC",
     None, 0, None, 1),
    (5, "Can you show the full profile of a specific customer by code or name?",
     "vw_q005_customer_full_profile_by_code_or_name", "v1", "APPROVED_LOGIC",
     None, 1, None, 1),
    (11, "Which items are dead stock or severe dead stock?",
     "vw_q011_items_dead_stock_or_severe_dead_stock", "v1", "DATA_SCIENCE_REVIEW_REQUIRED",
     None, 0, None, 1),
]


def build_fixture_db(path: Path, extra_sales_rows: int = 0) -> Path:
    if path.exists():
        # Wipe existing objects in-place rather than deleting the file.
        # On Windows, a file can't be unlinked while another process (e.g.
        # a ReadOnlyExecutor's still-open connection pool, as in
        # test_db_watcher.py's refresh-simulation test) holds it open —
        # POSIX allows this, Windows does not. DROPing every table/view and
        # rebuilding the schema in the same file achieves the same "fresh
        # DB" result on every platform, including under an open pool.
        wipe_conn = sqlite3.connect(path)
        try:
            names = [
                row[0]
                for row in wipe_conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for name in names:
                kind = wipe_conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()[0]
                wipe_conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
            wipe_conn.commit()
        finally:
            wipe_conn.close()
    conn = sqlite3.connect(path)
    try:
        for ddl in DDL:
            conn.execute(ddl)
        conn.executemany(
            f"INSERT INTO fact_ai_sales_net VALUES ({','.join('?' * 26)})", SEED_SALES
        )
        for i in range(extra_sales_rows):
            row = list(SEED_SALES[0])
            row[11] = f"INV-X{i}"
            row[12] = f"KX{i}"
            conn.execute(f"INSERT INTO fact_ai_sales_net VALUES ({','.join('?' * 26)})", row)
        conn.executemany(
            "INSERT INTO chatbot_question_view_registry "
            "(question_id, canonical_question, view_name, formula_version, assumption_status,"
            " time_scope_rule, requires_endpoint_filter, implementation_note, enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            SEED_REGISTRY,
        )
        conn.execute(
            "INSERT INTO dim_b3_item (item_code, item_name, item_group_code) VALUES "
            "('A100','Brake Pad','G1'), ('A200','Oil Filter','G2')"
        )
        conn.execute(
            "INSERT INTO batch_13_business_glossary VALUES "
            "('dead stock','Items with no sales for 12+ months'),"
            "('net revenue','Invoice revenue minus credit notes')"
        )
        conn.commit()
    finally:
        conn.close()
    return path