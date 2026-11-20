"""FastAPI app. Run locally:  cd backend && uvicorn app.main:app --reload --port 8000"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# backend/.env (gitignored) for local secrets such as LLM_API_KEY. Real env vars win.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.api.errors import install_error_handlers  # noqa: E402
from app.api.routes import router  # noqa: E402
from app.db.connection import connect, init_schema_with_retry  # noqa: E402
from app.observability.logging import setup_logging  # noqa: E402
from app.observability.metrics import install_middleware, metrics_response  # noqa: E402

log = logging.getLogger("app")


def db_init_retry_seconds() -> float:
    """DB_INIT_RETRY_SECONDS: seconds to wait for the database at startup. 0 (default) = one try."""
    try:
        return max(float(os.environ.get("DB_INIT_RETRY_SECONDS", "0") or 0), 0.0)
    except ValueError:
        return 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_schema_with_retry(db_init_retry_seconds(), connect_fn=connect)
        log.info("database schema ready")
    except Exception as e:  # the app still starts; /health reports the DB as down
        log.warning("could not initialise the database schema: %s", type(e).__name__)
    yield


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Timecard Exception Resolution API",
        version="0.1.0",
        description="Detect timecard exceptions, propose grounded fixes, require manager approval.",
        lifespan=lifespan,
    )
    install_middleware(app)
    install_error_handlers(app)
    app.include_router(router)

    @app.get("/health", tags=["ops"])
    def health(request: Request) -> JSONResponse:
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            return JSONResponse({"status": "ok", "database": "ok"})
        except Exception:
            return JSONResponse({"status": "degraded", "database": "unreachable"}, status_code=503)

    @app.get("/metrics", tags=["ops"])
    def metrics():
        return metrics_response()

    return app


app = create_app()
