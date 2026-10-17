"""Shared fixtures: a temp DATA_DIR and SQLite file, and a real app client.

Settings isolation: `data_dir` sets DATA_DIR and DATABASE_URL with monkeypatch
(environment variables win over backend/.env in pydantic-settings), clears the
lru_cache of get_settings() and resets the lazy engine. monkeypatch restores the
environment afterwards. Entering TestClient as a context manager runs the app
lifespan, which starts the background runner.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.settings import get_settings


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    # A developer's backend/.env may set the access gate; tests start with it off (test_access turns it on).
    monkeypatch.setenv("ACCESS_PASSWORD", "")
    monkeypatch.setenv("DEMO_PUBLIC", "false")
    get_settings.cache_clear()
    db.reset_engine()
    yield root
    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def client(data_dir: Path) -> Iterator["TestClient"]:
    # Imported here, not at module level: engine and LLM tests must not need the web app.
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
