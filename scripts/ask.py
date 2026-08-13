#!/usr/bin/env python3
"""Manual M4 gate helper: ask one question end-to-end against the real DB.

    ELIARA_ANTHROPIC_API_KEY=... python scripts/ask.py \
        --db data/eliara_production_clean.db "Who are the top 10 customers?"
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.audit import AuditTrail  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.discovery.service import build_discovery  # noqa: E402
from app.execution.executor import ReadOnlyExecutor  # noqa: E402
from app.llm.anthropic_client import AnthropicClient  # noqa: E402
from app.orchestrator.conversation import InMemoryConversationStore  # noqa: E402
from app.orchestrator.orchestrator import Orchestrator  # noqa: E402
from app.prompts.loader import PromptManager  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--session", default="cli")
    parser.add_argument("question", nargs="*", help="omit for interactive chat mode")
    args = parser.parse_args()

    settings = Settings(db_path=args.db)
    executor = ReadOnlyExecutor(
        settings.db_path, query_timeout_s=settings.query_timeout_s, max_rows=settings.max_rows
    )
    try:
        index, retriever = build_discovery(executor, settings)
        orchestrator = Orchestrator(
            retriever=retriever, index=index, executor=executor, prompts=PromptManager(),
            conversations=InMemoryConversationStore(), llm=AnthropicClient(settings),
            settings=settings, audit=AuditTrail(settings.audit_dir, settings.audit_enabled),
        )
        from app.core.errors import EliaraError

        async def ask_once(question: str) -> None:
            try:
                outcome = await orchestrator.handle(args.session, question)
            except EliaraError as exc:
                print(f"\n[error] {exc.public_message}")
                return
            print(f"\n[decision={outcome.decision} view={outcome.view_used} "
                  f"sql={outcome.sql_generated} latency={outcome.latency_ms}ms "
                  f"tokens={outcome.input_tokens}/{outcome.output_tokens}]\n")
            print(outcome.answer)

        if args.question:
            await ask_once(" ".join(args.question))
        else:
            print("Interactive mode — same session, follow-ups supported. Ctrl+C to exit.")
            while True:
                try:
                    question = input("\nYou: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if question:
                    await ask_once(question)
        return 0
    finally:
        executor.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
