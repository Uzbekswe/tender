"""Prompt loader (ARCHITECTURE §6.4).

Prompts live in ``<prompts_dir>/<name>/v<N>.md``. Each file is a Jinja2 template
that may be split into two sections:

    ## system
    System text, used as-is (not rendered).

    ## user
    User template, rendered with the call's variables (StrictUndefined).

A file with neither heading is a user template with an empty system prompt.
Other ``##`` lines are ordinary Markdown inside the body.

Versions: the highest vN is used unless a version is passed (the caller reads
``[prompt_versions]`` from models.toml). Never edit an existing vN.md; add vN+1.md,
because the version string is stored on every call and on every run.
"""

import re
from collections.abc import Mapping
from pathlib import Path

import jinja2

from app.settings import get_settings

_PROMPT_FILE = re.compile(r"^v(\d+)\.md$")
_HEADINGS = {"## system": "system", "## user": "user"}
_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)


class PromptError(LookupError):
    """Missing, malformed or unrenderable prompt."""


def parse_version(version: str | int) -> int:
    """Accept 2, "2", "v2" or "demo@v2" and return 2."""
    text = str(version).rsplit("@", 1)[-1].strip().lstrip("vV")
    if not text.isdigit():
        raise PromptError(f"bad prompt version '{version}'")
    return int(text)


def load_prompt(
    name: str,
    version: str | int | None = None,
    prompts_dir: Path | None = None,
) -> tuple[str, str, str]:
    """Return (version string like "read_page@v1", system_text, user_template)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PromptError(f"bad prompt name '{name}'")
    if prompts_dir is None:
        prompts_dir = get_settings().prompts_dir
    versions = _versions_in(prompts_dir / name)
    if not versions:
        raise PromptError(f"no prompt versions for '{name}' in {prompts_dir / name}")
    number = max(versions) if version is None else parse_version(version)
    if number not in versions:
        raise PromptError(f"prompt '{name}' has no v{number} (found: {sorted(versions)})")
    system, user = parse_prompt_file(versions[number].read_text(encoding="utf-8"))
    return f"{name}@v{number}", system, user


def parse_prompt_file(text: str) -> tuple[str, str]:
    """Split a prompt file into (system, user) using the ## system / ## user headings."""
    sections: dict[str, list[str]] = {}
    preamble: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        key = _HEADINGS.get(line.strip().lower())
        if key is not None:
            if key in sections:
                raise PromptError(f"duplicate '## {key}' section")
            sections[key] = []
            current = key
        elif current is None:
            preamble.append(line)
        else:
            sections[current].append(line)
    if not sections:
        return "", text.strip()
    if any(line.strip() for line in preamble):
        raise PromptError("text before the first '## system' / '## user' heading")
    if "user" not in sections:
        raise PromptError("missing '## user' section")
    system = "\n".join(sections.get("system", [])).strip()
    return system, "\n".join(sections["user"]).strip()


def render(template: str, variables: Mapping[str, object]) -> str:
    """Render a user template. Undefined variables are an error, not an empty string."""
    try:
        return _ENV.from_string(template).render(dict(variables))
    except jinja2.TemplateError as exc:
        raise PromptError(f"cannot render prompt: {exc}") from exc


def _versions_in(folder: Path) -> dict[int, Path]:
    if not folder.is_dir():
        return {}
    found: dict[int, Path] = {}
    for path in folder.iterdir():
        match = _PROMPT_FILE.match(path.name)
        if match and path.is_file():
            found[int(match.group(1))] = path
    return found


def call_tag(task: str, prompt: str) -> str:
    """Provider, model and prompt version a task would use now, e.g. "claude_cli:claude-haiku-5-5|match@v2".

    The model is resolved through [tiers] (a task names a tier), and the provider is the
    effective one (LLM_PROVIDER overrides models.toml). Stages put this into the keys of
    their stored answers, so a new prompt version, a different model or a different
    provider makes them ask again instead of silently reusing an old answer.
    """
    from app.settings import load_toml

    settings = get_settings()
    config = load_toml(settings.models_config)
    value = config.get("tasks", {}).get(task, "")
    model = config.get("tiers", {}).get(value, value)
    provider = (settings.llm_provider or config.get("provider", "")).strip()
    version, _, _ = load_prompt(prompt, version=config.get("prompt_versions", {}).get(prompt))
    return f"{provider}:{model}|{version}"
