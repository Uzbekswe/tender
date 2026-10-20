"""bakeoff.py must honour LLM_PROVIDER, not only the models file. No model is called."""

import importlib.util
import sys
from pathlib import Path

from app.settings import get_settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bakeoff.py"


def load_bakeoff():
    spec = importlib.util.spec_from_file_location("bakeoff_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


async def test_bakeoff_uses_llm_provider_and_the_agent_cache_name(data_dir, monkeypatch):
    bakeoff = load_bakeoff()
    seen = {}

    class FakeProvider:
        pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            seen["provider_name"] = kwargs["provider_name"]

    monkeypatch.setattr(bakeoff, "get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(bakeoff, "DefaultLLMClient", FakeClient)
    monkeypatch.setenv("LLM_PROVIDER", "opencode_cli")
    monkeypatch.setenv("OPENCODE_AGENT", "plan")
    monkeypatch.setenv(
        "MODELS_CONFIG",
        str(Path(__file__).resolve().parents[2] / "config" / "models.opencode.toml"),
    )
    get_settings.cache_clear()
    try:
        rows = await bakeoff.run_bakeoff(["opencode/glm-5.3-flash-free"], [])
    finally:
        get_settings.cache_clear()

    assert rows == []
    assert seen["provider_name"] == "opencode_cli:plan"
