"""App settings from environment / .env (SHARED CONTRACT)."""

import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    data_dir: Path = BACKEND_DIR.parent / "data"
    database_url: str = ""  # default: sqlite file inside data_dir
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    # LLM provider: claude_cli (default) | anthropic_api | handoff | openrouter. Empty = models.toml `provider`.
    llm_provider: str = ""
    llm_concurrency: int = 0  # max calls in flight; 0 = models.toml [limits].parallel_calls
    claude_cli_path: str = "claude"  # binary of Claude Code, looked up on PATH unless absolute
    models_config: Path = BACKEND_DIR / "config" / "models.toml"
    scoring_config: Path = BACKEND_DIR / "config" / "scoring.toml"
    prompts_dir: Path = BACKEND_DIR / "prompts"
    render_dpi: int = 150  # page PNGs for the viewer
    vision_dpi: int = 200  # page PNGs sent to the vision model
    # Access gate (app/web/auth.py). Empty password = no gate, the app is open as before.
    access_password: str = ""
    demo_public: bool = False  # with the gate on: start page, demo projects and demo sample download stay open
    session_secret: str = ""  # signs the login cookie; empty = derived from access_password
    # Accounts (app/web/auth.py, app/accounts.py). ACCOUNTS=1 replaces the shared password: visitors sign up
    # with an email and their own password, and each project belongs to the account that created it.
    accounts: bool = False
    signup_code: str = ""  # invite code a visitor must type to sign up; empty = anyone may sign up

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'app.db'}"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)
