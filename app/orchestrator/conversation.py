"""Conversation memory: message history plus a working set of recent results.

The original store held `Message(role, content)` only — text. When a turn
ended, the `QueryResult` behind it was thrown away. A follow-up like "sort
those by margin" had nothing to sort: the model had to reconstruct the whole
question from the text history and regenerate SQL from scratch for data it
already had a message ago.

`ResultEntry` keeps the last few results (capped, TTL-evicted with the rest
of the session) so a refinement can operate on them directly — see
`app/execution/refine.py`. The working set stores the QueryResult itself, not
a rendering of it, so a later refinement sees every row, not the payload-
truncated slice the model was shown.
"""

import threading
import time
from collections import deque

from pydantic import BaseModel

from app.core.models import QueryResult


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ResultEntry(BaseModel):
    label: str  # the question that produced this result
    result: QueryResult
    source: str  # view name / "custom query" / playbook title — for display only


class InMemoryConversationStore:
    def __init__(
        self, history_size: int = 5, ttl_min: int = 120, working_set_size: int = 3
    ) -> None:
        self._history_size = history_size
        self._ttl_s = ttl_min * 60
        self._working_set_size = working_set_size
        self._sessions: dict[str, tuple[deque[Message], deque[ResultEntry], float]] = {}
        self._lock = threading.Lock()

    def _entry(self, session_id: str) -> tuple[deque[Message], deque[ResultEntry]]:
        history, results, _ = self._sessions.get(
            session_id,
            (
                deque(maxlen=self._history_size),
                deque(maxlen=self._working_set_size),
                0.0,
            ),
        )
        return history, results

    def get_history(self, session_id: str) -> list[Message]:
        with self._lock:
            history, _ = self._entry(session_id)
            return list(history)

    def append(self, session_id: str, message: Message) -> None:
        with self._lock:
            history, results = self._entry(session_id)
            history.append(message)
            self._sessions[session_id] = (history, results, time.monotonic())

    def remember_result(self, session_id: str, result: QueryResult, label: str) -> None:
        """Keep a result in the working set. Empty results aren't worth a
        slot — "no data" has nothing to refine."""
        if not result.rows:
            return
        with self._lock:
            history, results = self._entry(session_id)
            results.append(ResultEntry(label=label, result=result, source=result.object_name))
            self._sessions[session_id] = (history, results, time.monotonic())

    def working_set(self, session_id: str) -> list[ResultEntry]:
        """Most recent first — index 0 is what "those" or "it" usually means."""
        with self._lock:
            _, results = self._entry(session_id)
            return list(reversed(results))

    def purge_expired(self) -> int:
        cutoff = time.monotonic() - self._ttl_s
        with self._lock:
            expired = [k for k, (_, _, ts) in self._sessions.items() if ts < cutoff]
            for key in expired:
                del self._sessions[key]
            return len(expired)
