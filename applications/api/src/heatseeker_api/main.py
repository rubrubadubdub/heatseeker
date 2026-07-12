"""FastAPI app: browser GUI (ADR-0009) + JSON API under /api/*.

Binds 127.0.0.1 only by default (spec §32.1). UI pages and API endpoints share the
same engine and call the same Python functions.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from heatseeker_common.db import create_db_engine
from heatseeker_common.health import check_health
from heatseeker_common.settings import Settings, get_settings
from heatseeker_source_registry.regions import load_regions_if_available

from heatseeker_api import api_entities, api_routes, ui_ai, ui_entities, ui_routes, ui_sources

logger = logging.getLogger("heatseeker.api")

_HERE = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_db_engine(settings)
    load_regions_if_available(engine)  # named regions are data (ADR-0012)

    app = FastAPI(title="Heatseeker", version="0.1.0", docs_url="/docs")
    app.state.settings = settings
    app.state.engine = engine

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.include_router(api_routes.router)
    app.include_router(api_entities.router)
    app.include_router(ui_ai.router)
    app.include_router(ui_sources.router)
    app.include_router(ui_entities.router)
    app.include_router(ui_routes.router)

    @app.get("/health", include_in_schema=False)
    def health_alias() -> JSONResponse:
        # Ops convenience alias for /api/health.
        report = check_health(engine, settings)
        return JSONResponse(report, status_code=200 if report["status"] == "ok" else 503)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        # Never leak stack traces to clients; full detail goes to the log.
        logger.exception("unhandled error", extra={"path": request.url.path})
        if request.url.path.startswith(("/api", "/health")):
            return JSONResponse({"error": "internal server error"}, status_code=500)
        return ui_routes.templates.TemplateResponse(
            request,
            "error.html",
            {"title": "Something went wrong", "detail": "See server logs for details."},
            status_code=500,
        )

    return app
