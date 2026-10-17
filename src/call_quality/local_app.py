"""Standalone customer-call quality API and frontend server."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from call_quality.api import router
from call_quality.store import init_db, recover_interrupted_jobs

app = FastAPI(
    title="Customer Call Quality Analyzer",
    version="1.0.0",
    description="Local transcription with structured customer-call quality analysis.",
)
app.mount("/app", StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend", html=True), name="frontend")
app.include_router(router)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    recover_interrupted_jobs()


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "Customer Call Quality Analyzer", "mode": "local-only", "docs": "/docs", "ui": "/app/"}
