"""Claude Haiku SQL generation — receives a structured request, returns SQL text.

Isolation guarantees (Phase 2/3 design): Haiku never sees conversation history,
the raw user message, prompts internals, or secrets — only the orchestrator's
task_description plus the schema slice.
"""

import re

from app.core.config import Settings
from app.core.logging import get_logger
from app.llm.anthropic_client import AnthropicClient, LLMResponse
from app.prompts.loader import PromptManager, RenderedPrompt
from app.sqlgen.schema_context import TableSlice

log = get_logger("sqlgen")

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_SQL_START_RE = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)


def _extract_sql(text: str) -> str:
    """Best-effort extraction of the SQL body from a model response.

    Handles fenced blocks and prose-wrapped SQL. If no SQL-looking start is
    found, the raw text is returned and the AST validator rejects it — which
    triggers the corrective retry with the rejection reason."""
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    match = _SQL_START_RE.search(text)
    if match:
        text = text[match.start():]
    return text.strip().rstrip("`").strip()


class SQLGenerator:
    def __init__(self, llm: AnthropicClient, prompts: PromptManager, settings: Settings) -> None:
        self._llm = llm
        self._prompts = prompts
        self._settings = settings

    async def generate(
        self,
        task_description: str,
        schema_slice: list[TableSlice],
        previous_error: str | None = None,
    ) -> tuple[str, LLMResponse]:
        prompt = self._prompts.render(
            "sqlgen_generate",
            max_rows=self._settings.max_rows,
            task_description=task_description,
            tables=[t.model_dump() for t in schema_slice],
        )
        if previous_error:
            prompt = RenderedPrompt(
                name=prompt.name, version=prompt.version, system=prompt.system,
                user=(
                    f"{prompt.user}\n\n## Correction\nYour previous SQL was rejected: "
                    f"{previous_error}\nReturn the corrected SQL only."
                ),
            )
        response = await self._llm.call(
            prompt,
            model=self._settings.sqlgen_model,
            max_tokens=800,
            temperature=0.0,
        )
        sql = _extract_sql(response.text)
        log.info("sql_generated", chars=len(sql), retry=bool(previous_error))
        return sql, response
