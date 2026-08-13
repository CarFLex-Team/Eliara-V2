"""Bounded reasoning loop — the "brain" layer over the analytical core.

The single-shot pipeline makes ONE decision and never sees what came back.
That is fine for "who are our top customers" and structurally incapable of
"why did margin drop in Q2" — which needs a result, a look at it, and a second
question formed from the first answer.

This loop keeps every safety property of the pipeline and changes only how
many times the governed surface is touched:

  - Tools are the SAME code paths the pipeline uses. `run_view` goes through
    `ReadOnlyExecutor.run_view` (identifier-validated, bound parameters);
    `run_sql` goes through Haiku and the sqlglot AST gate; `search_catalogue`
    returns only objects the metadata index admits. The model gains iterations,
    not reach.
  - Every loop is bounded three ways — steps, wall clock, tokens. Whichever
    binds first, the loop stops and still produces an answer from what it has.
    A reasoning agent that returns nothing is worse than a briefer that
    returns something.
  - The trace is the provenance. A composed answer cannot name one approved
    view in `endpoint_used`, so the ordered list of governed calls takes that
    role and travels with the answer.

Two capabilities that were invisible plumbing become real affordances here.
Entity resolution was a silent substitution or a canned clarification; as a
tool the model can check a name BEFORE committing to a filter. The glossary
was keyword-injected on substring match; as a lookup it gets consulted when
the model is actually unsure.

Deliberately NOT a replacement for playbooks. A recurring review with fixed,
human-chosen steps is cheaper, faster and more auditable as a playbook. This
handles the questions nobody wrote a playbook for.
"""

import time
from collections.abc import Awaitable, Callable

from app.core.config import Settings
from app.core.errors import SQLValidationError
from app.core.logging import get_logger
from app.core.models import QueryResult
from app.execution.aggregate import summarise
from app.execution.formatter import to_llm_payload
from app.execution.refine import RefineError, RefineSpec, apply_refinement
from app.orchestrator.agent_models import AgentStep, AgentTrace, ToolCall
from app.orchestrator.verification import verify
from app.sqlgen.schema_context import build_slice
from app.sqlgen.validator import validate_sql

log = get_logger("agent")

# User-facing loading text for each tool's "stage" SSE event — internal
# action names never reach the client, matching the same convention the
# orchestrator uses for its own stage events.
_AGENT_STAGE_TEXT = {
    "search_catalogue": "Searching available data...",
    "resolve_entity": "Looking that up...",
    "run_view": "Querying your data...",
    "run_sql": "Running a custom query...",
    "glossary": "Checking definitions...",
    "use_previous_result": "Refining previous results...",
}

# Per-observation payload budget. Several results share one context window, so
# each gets a slice rather than the whole thing. Stats cover every row anyway.
_OBSERVATION_CHARS = 900
_OBSERVATION_ROW_CAP = 25
_CATALOGUE_RESULTS = 5


class ReasoningAgent:
    """Runs one user turn as a bounded sequence of governed tool calls."""

    def __init__(
        self,
        *,
        retriever,
        index,
        executor,
        prompts,
        llm,
        settings: Settings,
        entities=None,
        sqlgen=None,
        result_cache=None,
    ) -> None:
        self._retriever = retriever
        self._index = index
        self._executor = executor
        self._prompts = prompts
        self._llm = llm
        self._settings = settings
        self._entities = entities
        self._sqlgen = sqlgen
        self._cache = result_cache

    # ------------------------------------------------------------------ loop

    async def run(
        self, message: str, history: list | None = None, data_as_of: str = "unknown",
        working_set: list | None = None,
        emit: Callable[[dict], Awaitable[None]] | None = None,
    ) -> tuple[str, AgentTrace]:
        """working_set: ResultEntry list from the session, most recent first —
        carried in so a turn can refine what an earlier turn already fetched,
        not just what this turn's own tool calls produce.

        emit: per-step progress events. The model's tool-call decisions are
        small JSON (~40 tokens) that can't be usefully token-streamed — the
        value here is showing each step as it completes ("checking margin...",
        "checking receivables..."), which is real information about what the
        loop is doing, not a synthetic progress bar. The forced-landing text
        (see _land) genuinely streams token-by-token, since that call IS
        free-text prose.
        """
        async def _emit(event: dict) -> None:
            if emit is not None:
                await emit(event)

        deadline = time.monotonic() + self._settings.agent_time_budget_s
        self._working_set = working_set or []
        # This turn's own results, addressable by use_previous_result at index
        # < len(turn results); the session's working set follows after.
        self._turn_results: list[tuple[str, QueryResult]] = []
        # Haiku's tokens are spent inside a tool call, so they are collected
        # here rather than at the loop's own structured_call sites.
        self._sqlgen_tokens = [0, 0]
        trace = AgentTrace()
        observations: list[str] = []
        results: list[QueryResult] = []

        for step_number in range(1, self._settings.agent_max_steps + 1):
            if time.monotonic() > deadline:
                trace.stopped_because = "time_budget"
                break
            if trace.input_tokens > self._settings.agent_token_budget:
                trace.stopped_because = "token_budget"
                break

            prompt = self._prompts.render(
                "agent_step",
                data_as_of=data_as_of,
                message=message,
                history=[m.model_dump() for m in (history or [])],
                observations=observations,
                step=step_number,
                steps_remaining=self._settings.agent_max_steps - step_number,
            )
            step, response = await self._llm.structured_call(
                prompt, AgentStep, model=self._settings.orchestrator_model
            )
            trace.input_tokens += response.input_tokens
            trace.output_tokens += response.output_tokens
            trace.steps_used = step_number

            if step.action == "answer":
                trace.stopped_because = "answered"
                trace.input_tokens += self._sqlgen_tokens[0]
                trace.output_tokens += self._sqlgen_tokens[1]
                answer = step.text or ""
                log.info(
                    "agent_answered",
                    steps=step_number,
                    calls=len(trace.calls),
                    views=trace.views_used,
                )
                return self._verified(answer, results, message), trace

            call, observation, result = await self._execute(step, step_number)
            await _emit({"type": "stage", "value": _AGENT_STAGE_TEXT.get(
                call.action, "Working on it..."
            )})
            trace.calls.append(call)
            observations.append(observation)
            if result is not None:
                results.append(result)
                self._turn_results.append((call.argument or call.action, result))
        else:
            trace.stopped_because = "step_budget"

        # Budget bound before the model chose to answer. Land it anyway — one
        # synthesis call over whatever was gathered.
        log.info(
            "agent_forced_landing",
            reason=trace.stopped_because,
            steps=trace.steps_used,
            calls=len(trace.calls),
        )
        answer, t_in, t_out = await self._land(message, observations, data_as_of, trace, emit=emit)
        trace.input_tokens += t_in + self._sqlgen_tokens[0]
        trace.output_tokens += t_out + self._sqlgen_tokens[1]
        return self._verified(answer, results, message), trace

    async def _land(
        self, message: str, observations: list[str], data_as_of: str, trace: AgentTrace,
        emit: Callable[[dict], Awaitable[None]] | None = None,
    ) -> tuple[str, int, int]:
        """Forced synthesis when a budget binds before the model finishes."""
        if not observations:
            message_text = (
                "I could not gather enough data to answer that within the time "
                "available. Narrowing it to a specific period, customer, or item "
                "usually helps."
            )
            return message_text, 0, 0
        prompt = self._prompts.render(
            "agent_synthesis",
            data_as_of=data_as_of,
            message=message,
            observations=observations,
            partial=True,
            char_budget=self._settings.answer_char_budget,
            max_bullets=self._settings.answer_max_bullets,
        )
        if emit is None:
            response = await self._llm.call(
                prompt, model=self._settings.orchestrator_model, max_tokens=900
            )
            return response.text, response.input_tokens, response.output_tokens

        parts: list[str] = []
        final = None
        async for chunk in self._llm.stream(
            prompt, model=self._settings.orchestrator_model, max_tokens=900
        ):
            if chunk.text:
                parts.append(chunk.text)
                await emit({"type": "token", "value": chunk.text})
            if chunk.done:
                final = chunk.response
        return "".join(parts), final.input_tokens, final.output_tokens

    def _verified(self, answer: str, results: list[QueryResult], message: str) -> str:
        """Ground figures against the UNION of every result in the turn.

        The single-shot path verifies against one result set. A composed answer
        legitimately mixes figures from several, so verifying against only the
        last one would flag correct numbers as fabricated — and a verifier that
        cries wolf is a verifier people learn to ignore.
        """
        if not results:
            return answer
        merged = self._merge(results)
        report = verify(
            answer, merged, question=message, strict=self._settings.verification_strict
        )
        if report.status == "warn":
            log.warning(
                "agent_verification_warn",
                checked=report.checked,
                grounded=report.grounded,
                ungrounded=report.ungrounded,
            )
            if self._settings.verification_strict:
                return answer + (report.caveat() or "")
        return answer

    @staticmethod
    def _merge(results: list[QueryResult]) -> QueryResult:
        """One synthetic result carrying every cell the answer may cite."""
        if len(results) == 1:
            return results[0]
        columns: list[str] = []
        rows: list[tuple] = []
        for result in results:
            columns.extend(result.columns)
            rows.extend(result.rows)
        return QueryResult(
            columns=columns or ["value"],
            rows=rows,
            row_count=len(rows),
            truncated=any(r.truncated for r in results),
            source="view",
            object_name="agent_turn",
            elapsed_ms=sum(r.elapsed_ms for r in results),
        )

    # ----------------------------------------------------------------- tools

    async def _execute(
        self, step: AgentStep, step_number: int
    ) -> tuple[ToolCall, str, QueryResult | None]:
        started = time.perf_counter()
        try:
            call, observation, result = await self._dispatch(step, step_number)
        except SQLValidationError as exc:
            # A rejection is information, not a failure — the model gets the
            # reason and can pick a different approach next iteration.
            call = ToolCall(
                step=step_number, action=step.action, status="rejected",
                detail=exc.internal_detail,
            )
            observation = f"[{step_number}] {step.action} REJECTED: {exc.internal_detail}"
            result = None
        except Exception as exc:  # noqa: BLE001 - a tool fault must not kill the turn
            log.warning(
                "agent_tool_failed", action=step.action, error=type(exc).__name__
            )
            call = ToolCall(
                step=step_number, action=step.action, status="error",
                detail=type(exc).__name__,
            )
            observation = f"[{step_number}] {step.action} failed: {type(exc).__name__}"
            result = None
        call.elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "agent_tool_call",
            step=step_number, action=call.action, status=call.status,
            rows=call.row_count, elapsed_ms=call.elapsed_ms,
        )
        return call, observation, result

    async def _dispatch(self, step: AgentStep, n: int):
        if step.action == "search_catalogue":
            return self._tool_search(step, n)
        if step.action == "resolve_entity":
            return self._tool_resolve(step, n)
        if step.action == "run_view":
            return self._tool_view(step, n)
        if step.action == "run_sql":
            return await self._tool_sql(step, n)
        if step.action == "glossary":
            return self._tool_glossary(step, n)
        if step.action == "use_previous_result":
            return self._tool_refine(step, n)
        raise ValueError(f"unknown action: {step.action}")

    def _tool_search(self, step: AgentStep, n: int):
        query = step.query or ""
        candidates = self._retriever.search(query, k=_CATALOGUE_RESULTS)
        lines = [
            f"- {c.view_name}"
            + (f" — {c.canonical_question}" if c.canonical_question else "")
            + (" [needs an entity filter]" if c.requires_endpoint_filter else "")
            for c in candidates
        ]
        call = ToolCall(
            step=n, action="search_catalogue", argument=query,
            status="ok" if candidates else "empty", row_count=len(candidates),
        )
        body = "\n".join(lines) if lines else "(nothing matched)"
        return call, f"[{n}] search_catalogue({query!r}):\n{body}", None

    def _tool_resolve(self, step: AgentStep, n: int):
        column, value = step.column or "", step.value or ""
        if self._entities is None:
            call = ToolCall(step=n, action="resolve_entity", argument=value, status="empty")
            return call, f"[{n}] resolve_entity: no entity index loaded", None
        outcome = self._entities.resolve(column, value)
        call = ToolCall(
            step=n, action="resolve_entity", argument=f"{column}={value}",
            status="ok" if outcome.usable else "empty", detail=outcome.status,
        )
        if outcome.status == "ambiguous":
            options = ", ".join(outcome.candidates)
            text = f"AMBIGUOUS — several records match: {options}. Ask the user which."
        elif outcome.status == "unknown":
            text = f'UNKNOWN — "{value}" is not in the {column} records.'
        else:
            text = f'resolves to "{outcome.value}" (use this exact value as the filter)'
        return call, f"[{n}] resolve_entity({column}={value!r}): {text}", None

    def _tool_view(self, step: AgentStep, n: int):
        name = step.view_name or ""
        meta = self._index.objects.get(name)
        if meta is None:
            raise SQLValidationError(internal_detail=f"{name!r} is not in the catalogue")

        valid = set(meta.columns)
        filters = {c: v for c, v in step.filters.items() if c in valid and v}
        result = self._run_cached(
            ("view", name, tuple(sorted(filters.items()))),
            lambda: self._executor.run_view(name, filters or None, limit=_OBSERVATION_ROW_CAP),
        )
        registry = meta.registry
        call = ToolCall(
            step=n, action="run_view", argument=name, view_name=name,
            status="ok" if result.rows else "empty", row_count=result.row_count,
            assumption_status=registry.assumption_status if registry else None,
        )
        return call, self._observation(n, f"run_view({name})", result, filters), result

    async def _tool_sql(self, step: AgentStep, n: int):
        """Same Haiku generator, same AST gate, same isolation as the pipeline.

        Haiku still sees only the task description and the schema slice — never
        the conversation, never the observations gathered so far.
        """
        if self._sqlgen is None:
            raise ValueError("sql generation is not configured")
        task = step.task_description or ""
        candidates = self._retriever.search(task, k=self._settings.top_k_views)
        schema_slice = build_slice(self._index, step.tables, candidates)
        if not schema_slice:
            raise SQLValidationError(internal_detail="no usable schema objects for that request")

        sql_text, sql_response = await self._sqlgen.generate(task, schema_slice)
        self._sqlgen_tokens[0] += sql_response.input_tokens
        self._sqlgen_tokens[1] += sql_response.output_tokens
        validated = validate_sql(sql_text, self._index.whitelist, self._settings.max_rows)
        result = self._run_cached(
            ("sql", validated.sql), lambda: self._executor.run_sql(validated.sql)
        )
        call = ToolCall(
            step=n, action="run_sql", argument=task,
            status="ok" if result.rows else "empty", row_count=result.row_count,
            generated_sql=validated.sql,
        )
        return call, self._observation(n, "run_sql", result, None), result

    def _tool_refine(self, step: AgentStep, n: int):
        """Sort/filter/limit a result already gathered — this turn's own tool
        calls first, then the session's working set. Same deterministic path
        as the pipeline's refine route: no query, no LLM for the data op."""
        pool = list(self._turn_results) + [
            (w.label, w.result) for w in self._working_set
        ]
        idx = step.refine_target if step.refine_target is not None else 0
        if not (0 <= idx < len(pool)):
            call = ToolCall(step=n, action="use_previous_result", status="empty")
            return call, f"[{n}] use_previous_result: nothing at index {idx}", None

        label, source_result = pool[idx]
        try:
            spec = RefineSpec.model_validate(step.refine or {})
            result = apply_refinement(source_result, spec)
        except RefineError as exc:
            call = ToolCall(step=n, action="use_previous_result", status="rejected",
                             detail=str(exc))
            return call, f"[{n}] use_previous_result REJECTED: {exc}", None

        call = ToolCall(
            step=n, action="use_previous_result", argument=f"[{idx}] {label}",
            status="ok" if result.rows else "empty", row_count=result.row_count,
        )
        return call, self._observation(n, f'use_previous_result("{label}")', result, None), result

    def _tool_glossary(self, step: AgentStep, n: int):
        term = (step.query or "").lower()
        glossary = getattr(self._index, "glossary", None) or {}
        hits = [f"- {k}: {v}" for k, v in glossary.items() if term and term in k.lower()]
        call = ToolCall(
            step=n, action="glossary", argument=term,
            status="ok" if hits else "empty", row_count=len(hits),
        )
        body = "\n".join(hits[:5]) if hits else "(no company definition on file)"
        return call, f"[{n}] glossary({term!r}):\n{body}", None

    # --------------------------------------------------------------- helpers

    def _run_cached(self, key: tuple, execute):
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        result = execute()
        if self._cache is not None:
            self._cache.set(key, result)
        return result

    @staticmethod
    def _observation(
        n: int, label: str, result: QueryResult, filters: dict | None
    ) -> str:
        if not result.rows:
            scope = f" with {filters}" if filters else ""
            return f"[{n}] {label}{scope}: NO MATCHING ROWS. Do not infer anything from this."
        payload, shown = to_llm_payload(result, max_chars=_OBSERVATION_CHARS)
        block = [f"[{n}] {label}: {result.row_count} rows"]
        block.append(payload)
        stats = summarise(result, shown)
        if stats:
            block.append(f"Totals over all {result.row_count} rows:\n{stats}")
        return "\n".join(block)
