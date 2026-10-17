"""FastAPI application: `uvicorn app.main:app --reload`.

Startup creates the tables (idempotent) and starts the background runner, which
also resumes any run left queued or running by a previous process.
CORS is not enabled: the UI is served from this same app.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.checklist import router as checklist_router
from app.api.customer import router as customer_router
from app.api.llm_status import router as llm_status_router
from app.api.projects import router as api_router
from app.api.results import router as results_router
from app.web.auth import AccessGate
from app.web.auth import router as auth_router
from app.web.routes import router as web_router
from app.db import init_db, session_scope
from app.runner import runner
import engine.pipeline  # noqa: F401  (registers reading stages)
import engine.tender_stages  # noqa: F401  (registers tender stages after reading)
import engine.bid_stages  # noqa: F401  (registers bid stages after reading)
import engine.score_stages  # noqa: F401  (registers the score stage after assess)
from engine.demo import load_fixture

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    # The pre-recorded AI answers for the demo projects: the demo then costs nothing and runs offline.
    with session_scope() as session:
        load_fixture(session)
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()


app = FastAPI(title="Tender-Sverka", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
app.include_router(checklist_router)
app.include_router(results_router)
app.include_router(customer_router)
app.include_router(llm_status_router)
app.include_router(auth_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.add_middleware(AccessGate)  # no-op unless ACCESS_PASSWORD is set (app/web/auth.py)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
