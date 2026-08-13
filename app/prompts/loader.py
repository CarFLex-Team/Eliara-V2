"""Externalized, versioned prompt management.

Prompts live as YAML files under app/prompts/templates/**; editing behavior
never requires touching code. Every render records name@version so LLM logs
are fully traceable to the exact prompt text used.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from app.core.errors import EliaraError
from app.core.logging import get_logger

log = get_logger("prompts")


class PromptError(EliaraError):
    public_message = "Internal configuration error."


@dataclass(frozen=True)
class RenderedPrompt:
    name: str
    version: int
    system: str
    user: str

    @property
    def tag(self) -> str:
        return f"{self.name}@v{self.version}"


class PromptManager:
    def __init__(
        self,
        templates_dir: Path | None = None,
        extra_dir: Path | None = None,
    ) -> None:
        self._dir = templates_dir or Path(__file__).parent / "shared" / "templates"
        self._extra_dir = extra_dir
        self._env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        self._templates: dict[str, dict[int, dict]] = {}
        self._load_all()

    @classmethod
    def for_company(cls, config) -> "PromptManager":
        """Loads the shared prompt library, then layers in
        ``config.prompts_dir`` (if set) with company templates taking
        precedence over shared ones on name+version collision."""
        return cls(extra_dir=getattr(config, "prompts_dir", None))

    def _load_all(self) -> None:
        self._load_dir(self._dir)
        # An optional second directory of company-specific prompts. Same
        # name+version overwrites the shared one — company overrides win.
        # No prompt in this codebase currently needs this (none hardcode
        # company data — company context is already supplied as Jinja2
        # render vars), but the mechanism is here for the day one does.
        if self._extra_dir is not None and self._extra_dir.exists():
            self._load_dir(self._extra_dir)
        log.info(
            "prompts_loaded",
            prompts={n: sorted(v) for n, v in self._templates.items()},
        )

    def _load_dir(self, directory: Path) -> None:
        for path in sorted(directory.rglob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            name, version = data.get("name"), int(data.get("version", 0))
            if not name or version < 1 or "user_template" not in data:
                raise PromptError(internal_detail=f"malformed prompt file: {path}")
            self._templates.setdefault(name, {})[version] = data

    def active_version(self, name: str) -> int:
        if name not in self._templates:
            raise PromptError(internal_detail=f"unknown prompt: {name}")
        return max(self._templates[name])

    def render(self, name: str, *, version: int | None = None, **vars) -> RenderedPrompt:
        version = version or self.active_version(name)
        try:
            data = self._templates[name][version]
        except KeyError:
            raise PromptError(internal_detail=f"unknown prompt {name} v{version}") from None
        system = self._env.from_string(data.get("system", "")).render(**vars)
        user = self._env.from_string(data["user_template"]).render(**vars)
        return RenderedPrompt(name=name, version=version, system=system.strip(), user=user.strip())
