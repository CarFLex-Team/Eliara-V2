#!/usr/bin/env python3
"""Verify the install is complete BEFORE building the image.

A partial file copy fails at container start with ModuleNotFoundError, one
missing module at a time — fix one, rebuild, hit the next. This checks
everything in a single pass, on the host, in a second.

    python scripts/check_install.py

Exit code 0 = safe to build. 1 = files missing.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every file added or modified in this round of work.
REQUIRED_NEW = [
    # --- code modules (missing one = ModuleNotFoundError at startup) ---
    "app/api/legacy.py",
    "app/api/v1/catalogue.py",
    "app/discovery/entity_resolver.py",
    "app/execution/aggregate.py",
    "app/execution/visualize.py",
    "app/orchestrator/playbooks.py",
    "app/orchestrator/verification.py",
    # --- playbook definitions (missing = playbooks silently unavailable) ---
    "app/orchestrator/definitions/business_review.yaml",
    "app/orchestrator/definitions/investigate_customer.yaml",
    "app/orchestrator/definitions/procurement_plan.yaml",
    "app/orchestrator/definitions/stock_action_plan.yaml",
    "app/orchestrator/definitions/supplier_review.yaml",
    # --- prompts (missing = silently runs an older version) ---
    "app/prompts/templates/orchestrator/answer_v4.yaml",
    "app/prompts/templates/orchestrator/answer_v5.yaml",
    "app/prompts/templates/orchestrator/intent_v5.yaml",
    # --- build ---
    ".dockerignore",
]

REQUIRED_MODIFIED = [
    "app/core/config.py",
    "app/core/models.py",
    "app/discovery/index.py",
    "app/discovery/metadata_loader.py",
    "app/discovery/models.py",
    "app/main.py",
    "app/orchestrator/decision_models.py",
    "app/orchestrator/orchestrator.py",
    "docker-compose.yml",
]

# A marker string proving the file is the NEW version, not the original.
MARKERS = {
    "app/core/config.py": "entity_index_include_facts",
    "app/core/models.py": "formula_version",
    "app/discovery/index.py": "formula_version",
    "app/discovery/metadata_loader.py": "registry_without_formula_version",
    "app/discovery/models.py": "formula_version",
    "app/main.py": "include_facts=settings.entity_index_include_facts",
    "app/orchestrator/decision_models.py": "run_playbook",
    "app/orchestrator/orchestrator.py": "_run_playbook_path",
    "docker-compose.yml": "backend",
}


def main() -> int:
    missing, stale = [], []

    for relative in REQUIRED_NEW:
        if not (ROOT / relative).exists():
            missing.append(relative)

    for relative in REQUIRED_MODIFIED:
        path = ROOT / relative
        if not path.exists():
            missing.append(relative)
            continue
        marker = MARKERS.get(relative)
        if marker and marker not in path.read_text(errors="ignore"):
            stale.append((relative, marker))

    if not missing and not stale:
        print(f"OK  {len(REQUIRED_NEW) + len(REQUIRED_MODIFIED)} files present and current.")
        print("Safe to build.")
        return 0

    if missing:
        print(f"MISSING ({len(missing)}) — copy these from the download:\n")
        for relative in missing:
            print(f"  {relative}")
        print()

    if stale:
        print(f"STALE ({len(stale)}) — present, but still the OLD version:\n")
        for relative, marker in stale:
            print(f"  {relative}  (expected to contain '{marker}')")
        print()

    print("Fix, then re-run. Do NOT build until this passes.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
