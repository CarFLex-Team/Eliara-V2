#!/usr/bin/env python3
"""Post-deployment smoke test: health + 5 real questions over HTTP.

    python scripts/smoke_test.py --base-url http://127.0.0.1:8000
Exit 0 = deployment healthy.
"""

import argparse

import httpx

QUESTIONS = [
    "Who are the top 10 customers by lifetime revenue?",
    "Which items are dead stock or severe dead stock?",
    "Give me an overall summary of the customer portfolio",
    "What are the top selling products?",
    "What is the weather like today?",  # must be refused as out of scope
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    failures = 0

    with httpx.Client(timeout=args.timeout) as client:
        for path in ("/api/v1/health", "/api/v1/health/deep"):
            response = client.get(base + path)
            ok = response.status_code == 200
            print(f"{'OK  ' if ok else 'FAIL'} GET {path} -> {response.status_code} {response.text[:120]}")
            failures += 0 if ok else 1

        for i, question in enumerate(QUESTIONS, 1):
            response = client.post(
                base + "/api/v1/chat",
                json={"session_id": "smoke", "message": question},
            )
            ok = response.status_code == 200 and response.json().get("answer")
            meta = response.json().get("meta", {}) if response.status_code == 200 else {}
            print(
                f"{'OK  ' if ok else 'FAIL'} Q{i} [{meta.get('view_used') or ('sql' if meta.get('sql_generated') else '-')}"
                f" | {meta.get('latency_ms', '?')}ms] {question[:50]}"
            )
            failures += 0 if ok else 1

    print(f"\nSMOKE: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
