# Prompt management

All prompts live in `app/prompts/templates/**` as versioned YAML. Behavior
changes NEVER require code changes.

## Current set

| name | active | purpose |
|---|---|---|
| orchestrator_intent | v3 | routing decision JSON (follow-up resolution, token-budgeted candidates) |
| orchestrator_answer | v2 | business answer (caution wording, no empty-set speculation) |
| sqlgen_generate | v2 | Haiku SELECT-only generation (must start with SELECT/WITH) |

## Changing a prompt

1. Copy the highest version file: `intent_v3.yaml` → `intent_v4.yaml`
2. Set `version: 4` inside, edit the text
3. Restart. The manager auto-activates the highest version.
4. Every LLM log line carries `prompt=name@vN` — you can correlate any behavior
   change to the exact prompt version that produced it.

Rollback = delete the newest file (or ship v5 with the old text). Old versions
stay renderable (`prompts.render(name, version=N)`), used in A/B checks.

## Guard rails

- `tests/unit/test_prompt_manager.py` renders every shipped template with its
  variables (contract test) — run `pytest` after any edit.
- Templates are sandboxed Jinja2 with StrictUndefined: a typo in a variable
  name fails loudly at render, not silently at runtime.
- After changing `orchestrator_intent`, rerun the retrieval-independent manual
  gate: the 3-step follow-up scenario and 2-3 endpoint-filter questions.
