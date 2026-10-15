"""FastAPI application: `uvicorn app.main:app --reload`.

Startup creates the tables (idempotent) and starts the background runner, which
also resumes any run left queued or running by a previous process.
CORS is not enabled: the UI is served from this same app.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.llm_status import router as llm_status_router
from app.api.projects import router as api_router
from app.db import init_db
from app.runner import runner
import engine.pipeline  # noqa: F401  (registers reading stages)
import engine.tender_stages  # noqa: F401  (registers tender stages after reading)
import engine.bid_stages  # noqa: F401  (registers bid stages after reading)
import engine.score_stages  # noqa: F401  (registers the score stage after assess)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()


app = FastAPI(title="Tender-Sverka", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
app.include_router(llm_status_router)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
