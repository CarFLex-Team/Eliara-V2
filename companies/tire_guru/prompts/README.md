# Tire Guru prompt overrides — none yet

This directory is intentionally empty. Tire Guru currently uses 100% of
the shared prompt library (`app/prompts/shared/templates/`) — no
Tire Guru-specific wording has been needed so far.

If a prompt ever needs Tire Guru-specific business terminology or wording,
add a same-named, same-versioned YAML file here (see
`PromptManager.for_company()` in `app/prompts/loader.py`) — it will take
precedence over the shared version for this company only.
