"""GET /api/v1/llm/status: which LLM provider and models are configured, and whether the Claude CLI is there.

No model is called. `claude --version` runs only for the claude_cli provider, with a 10 s timeout.
"""

from fastapi import APIRouter

from app import settings as app_settings
from llm.providers.claude_cli import cli_found, cli_version

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])


@router.get("/status")
def llm_status() -> dict:
    settings = app_settings.get_settings()
    config = app_settings.load_toml(settings.models_config)
    provider = (settings.llm_provider or config.get("provider") or "claude_cli").strip()
    path = settings.claude_cli_path
    return {
        "provider": provider,
        "models": dict(config.get("tiers", {})),
        "cli_found": cli_found(path),
        "cli_version": cli_version(path) if provider == "claude_cli" else None,
    }
