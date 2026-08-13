# Beta company export

- beta-config.yaml — Beta's entry from companies.yaml (config: db path,
  scan views, playbook/prompt dirs, boundaries table)
- playbooks/ — Beta's 5 playbook definitions (from companies/beta/playbooks/)
- Prompts: none. companies/beta/prompts/ is empty by design — Beta uses
  100% shared prompts from app/prompts/shared/templates/; none of the
  original prompt YAMLs contained Beta-specific wording, so nothing has
  been forked into a company-specific override yet.
