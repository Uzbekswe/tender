"""GET /api/v1/llm/status: provider, tier models and Claude CLI presence (no model call)."""

from fastapi.testclient import TestClient


def test_llm_status_reports_provider_tiers_and_cli(client: TestClient, monkeypatch):
    import app.api.llm_status as status_module

    monkeypatch.setattr(status_module, "cli_found", lambda path: True)
    monkeypatch.setattr(status_module, "cli_version", lambda path: "2.1.300 (Claude Code)")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from app.settings import get_settings
    get_settings.cache_clear()

    body = client.get("/api/v1/llm/status").json()

    assert body["provider"] == "claude_cli"
    assert body["models"] == {"easy": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "hard": "claude-opus-5-5"}
    assert body["cli_found"] is True
    assert body["cli_version"] == "2.1.300 (Claude Code)"


def test_llm_status_skips_cli_version_for_other_providers(client: TestClient, monkeypatch):
    import app.api.llm_status as status_module

    called = []
    monkeypatch.setattr(status_module, "cli_found", lambda path: False)
    monkeypatch.setattr(status_module, "cli_version", lambda path: called.append(path) or "x")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic_api")
    from app.settings import get_settings
    get_settings.cache_clear()

    body = client.get("/api/v1/llm/status").json()

    assert body["provider"] == "anthropic_api"
    assert body["cli_version"] is None
    assert called == []


def test_llm_status_reports_the_opencode_cli_and_agent(client: TestClient, monkeypatch):
    import app.api.llm_status as status_module
    from app.settings import get_settings

    monkeypatch.setattr(status_module, "opencode_cli_found", lambda path: True)
    monkeypatch.setattr(status_module, "opencode_cli_version", lambda path: "2.0.32")
    monkeypatch.setenv("LLM_PROVIDER", "opencode_cli")
    monkeypatch.setenv(
        "MODELS_CONFIG",
        str(status_module.app_settings.BACKEND_DIR / "config" / "models.opencode.toml"),
    )
    get_settings.cache_clear()

    body = client.get("/api/v1/llm/status").json()

    assert body["provider"] == "opencode_cli"
    assert body["models"]["easy"] == "opencode/glm-5.3-flash-free"
    assert body["opencode_cli_found"] is True
    assert body["opencode_cli_version"] == "2.0.32"
    assert body["opencode_agent"] == "plan"
