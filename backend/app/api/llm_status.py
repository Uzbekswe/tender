"""GET /api/v1/llm/status: which LLM provider and models are configured, and whether the CLI is there.

No model is called. `claude --version` and `opencode --version` run only for their provider,
with a 10 s timeout.
"""

from fastapi import APIRouter

from app import settings as app_settings
from llm.providers.claude_cli import cli_found, cli_version
from llm.providers.opencode_cli import cli_found as opencode_cli_found
from llm.providers.opencode_cli import cli_version as opencode_cli_version
from llm.service import provider_name

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])


@router.get("/status")
def llm_status() -> dict:
    settings = app_settings.get_settings()
    config = app_settings.load_toml(settings.models_config)
    provider = provider_name()
    path = settings.claude_cli_path
    opencode_path = settings.opencode_cli_path
    return {
        "provider": provider,
        "models": dict(config.get("tiers", {})),
        "cli_found": cli_found(path),
        "cli_version": cli_version(path) if provider == "claude_cli" else None,
        "opencode_cli_found": opencode_cli_found(opencode_path),
        "opencode_cli_version": (
            opencode_cli_version(opencode_path) if provider == "opencode_cli" else None
        ),
        "opencode_agent": settings.opencode_agent if provider == "opencode_cli" else None,
    }
