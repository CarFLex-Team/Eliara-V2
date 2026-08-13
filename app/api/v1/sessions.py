"""GET /api/v1/sessions/{id} — the last five messages (debugging aid).

Conversation state is scoped by company_id + session_id: each company has
its own InMemoryConversationStore, so the same session_id under two
different companies never resolves to the same history.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.deps import get_company_context

router = APIRouter(tags=["sessions"])


class SessionHistory(BaseModel):
    company_id: str
    session_id: str
    messages: list[dict]


@router.get("/sessions/{session_id}", response_model=SessionHistory)
async def get_session(request: Request, session_id: str, company_id: str) -> SessionHistory:
    ctx = get_company_context(request, company_id)
    messages = [m.model_dump() for m in ctx.conversations.get_history(session_id)]
    return SessionHistory(company_id=company_id, session_id=session_id, messages=messages)
