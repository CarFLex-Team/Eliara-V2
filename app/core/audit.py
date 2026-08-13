"""Separate audit trail — accountability log, distinct from operational logs.

One JSONL file per day, per company, under ``audit_dir/<company_id>/``.
Records who asked what, how it was routed, and what was answered. Writing
is best-effort: an audit failure must never break a user request (it is
logged as an operational warning instead).

Multi-company note: a single shared ``AuditTrail`` instance is used across
all companies (it is stateless apart from its directory-creation lock), and
every record is filed under that company's own subdirectory — chosen over
one-instance-per-company so operators keep a single object to reason about
while still getting full filesystem separation between companies' audit
data.
"""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("audit")


class AuditTrail:
    def __init__(self, audit_dir: Path, enabled: bool = True) -> None:
        self._dir = Path(audit_dir)
        self._enabled = enabled
        self._lock = threading.Lock()
        self._known_dirs: set[str] = set()
        if enabled:
            self._dir.mkdir(parents=True, exist_ok=True)

    def _company_dir(self, company_id: str) -> Path:
        path = self._dir / company_id
        if company_id not in self._known_dirs:
            path.mkdir(parents=True, exist_ok=True)
            self._known_dirs.add(company_id)
        return path

    def _current_file(self, company_id: str) -> Path:
        return self._company_dir(company_id) / f"audit-{datetime.now(UTC):%Y-%m-%d}.jsonl"

    def record(
        self,
        *,
        company_id: str,
        session_id: str,
        question: str,
        decision: str,
        view_used: str | None,
        generated_sql: str | None,
        cache_hit: bool,
        latency_ms: int,
        input_tokens: int,
        output_tokens: int,
        answer: str,
    ) -> None:
        if not self._enabled:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "unix_ts": time.time(),
            "company_id": company_id,
            "session_id": session_id,
            "question": question,
            "decision": decision,
            "view_used": view_used,
            "generated_sql": generated_sql,
            "cache_hit": cache_hit,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "answer": answer,
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock, self._current_file(company_id).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            log.warning("audit_write_failed", company_id=company_id, exc_info=True)
