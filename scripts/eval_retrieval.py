#!/usr/bin/env python3
"""M2 quality gate: retrieval accuracy over the 90 canonical registry questions.

Run against the REAL production database with the REAL bge model:

    python scripts/eval_retrieval.py --db data/eliara_production_clean.db

First run downloads BAAI/bge-base-en-v1.5 (~440MB) from HuggingFace and caches
corpus embeddings under data/cache/. Exits non-zero if below thresholds
(top-3 >= 0.95, top-1 >= 0.85) so it can guard CI.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.discovery.embedder import EmbeddingCache, build_embedder  # noqa: E402
from app.discovery.evaluation import canonical_items, evaluate  # noqa: E402
from app.discovery.index import MetadataIndex  # noqa: E402
from app.discovery.metadata_loader import MetadataLoader  # noqa: E402
from app.discovery.search import HybridRetriever  # noqa: E402
from app.execution.executor import ReadOnlyExecutor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--backend", default="bge", choices=["bge", "hashing", "none"])
    parser.add_argument("--model", default="BAAI/bge-base-en-v1.5")
    parser.add_argument("--cache-dir", default="data/cache")
    parser.add_argument("--paraphrases", help="JSON: {question_id: paraphrase}")
    parser.add_argument("--min-top1", type=float, default=0.85)
    parser.add_argument("--min-top3", type=float, default=0.95)
    args = parser.parse_args()

    executor = ReadOnlyExecutor(args.db, query_timeout_s=60)
    try:
        objects, registry, glossary, fp = MetadataLoader(executor).load()
        index = MetadataIndex(objects, registry, glossary, fp)
        embedder = build_embedder(args.backend, args.model)
        retriever = HybridRetriever(index, embedder, EmbeddingCache(Path(args.cache_dir)))
        print(f"objects={len(objects)} registry={len(registry)} mode={retriever.mode}\n")

        report = evaluate(retriever, canonical_items(registry), set(objects))
        print("== CANONICAL QUESTIONS ==")
        print(f"evaluated={report.total} skipped={report.skipped_missing_view}")
        print(f"top-1: {report.top1_accuracy:.1%}   top-3: {report.top3_accuracy:.1%}")
        for miss in report.misses:
            print(f"  MISS q{miss.question_id:03d}: expected {miss.expected_view}")
            print(f"       got {miss.got_top3}")

        gate_ok = (
            report.top1_accuracy >= args.min_top1 and report.top3_accuracy >= args.min_top3
        )

        if args.paraphrases:
            mapping = {e.question_id: e.view_name for e in registry}
            para = json.loads(Path(args.paraphrases).read_text())
            items = [
                (int(qid), text, mapping[int(qid)])
                for qid, text in para.items()
                if int(qid) in mapping
            ]
            p_report = evaluate(retriever, items, set(objects))
            print("\n== PARAPHRASES ==")
            print(f"evaluated={p_report.total}  top-1: {p_report.top1_accuracy:.1%}"
                  f"   top-3: {p_report.top3_accuracy:.1%}")
            for miss in p_report.misses:
                print(f"  MISS q{miss.question_id:03d}: '{miss.query}' -> {miss.got_top3}")

        print(f"\nGATE (top-1>={args.min_top1:.0%}, top-3>={args.min_top3:.0%}): "
              f"{'PASS' if gate_ok else 'FAIL'}")
        return 0 if gate_ok else 1
    finally:
        executor.close()


if __name__ == "__main__":
    raise SystemExit(main())
