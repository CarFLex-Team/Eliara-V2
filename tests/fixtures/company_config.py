"""Test helper for the multi-company registry.

Tests that used to point the single global database at a fixture via
``ELIARA_DB_PATH`` now register that fixture as one company ("beta") in a
throwaway ``companies.yaml`` and point ``ELIARA_COMPANIES_CONFIG`` at it —
this is the direct multi-company equivalent of the old single-DB env-var
override.
"""

from pathlib import Path

import yaml


def write_companies_yaml(
    tmp_path: Path,
    db_path: Path,
    *,
    company_id: str = "beta",
    display_name: str = "Beta",
    scan_views: list[str] | None = None,
) -> Path:
    config = {
        "companies": {
            company_id: {
                "display_name": display_name,
                "db_path": str(db_path),
                "scan_views": scan_views or [],
            }
        }
    }
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path
