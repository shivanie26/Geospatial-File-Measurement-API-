"""FastAPI application factory.

Run with:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.api.routes import router
from app.config import Settings, get_settings
from app.services.geospatial_service import kml_driver_available
from app.services.storage_service import StorageService
from app.utils.exceptions import AppError

logger = logging.getLogger("geo_api")

_BODY_OVERHEAD = 1024 * 1024  # multipart framing allowance on top of the file limit
_HTTP_CODES = {
    400: "BAD_REQUEST", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE", 415: "UNSUPPORTED_MEDIA_TYPE", 422: "VALIDATION_ERROR",
}


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content={"error": body})


class MaxBodySizeMiddleware:
    """Reject requests whose declared Content-Length exceeds the limit before parsing the body.

    The exact per-file limit is enforced again while the upload is streamed to disk.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length":
                    try:
                        too_large = int(value) > self.max_bytes
                    except ValueError:
                        too_large = False
                    if too_large:
                        response = _error(413, "FILE_TOO_LARGE", "The request body exceeds the maximum upload size.")
                        await response(scope, receive, send)
                        return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    storage = StorageService(settings.storage_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        storage.ensure_dirs()
        if not kml_driver_available():
            logger.warning("GDAL KML driver unavailable: KML uploads will be rejected with HTTP 501.")
        yield

    app = FastAPI(
        title="Geospatial File Measurement API",
        version=__version__,
        description=(
            "Upload a KML file or a zipped Shapefile and get per-feature area (m², km²) and "
            "length (m, km), computed in an appropriate projected CRS."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.storage = storage
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_upload_bytes + _BODY_OVERHEAD)
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """Send people who open the bare address straight to the Swagger UI."""
        return RedirectResponse(url="/docs")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/health", tags=["health"], summary="Liveness and capability check")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "kml_supported": kml_driver_available()}

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("%s: %s", exc.code, exc.message)
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return _error(422, "VALIDATION_ERROR", "The request is invalid.", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, _HTTP_CODES.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _error(500, "INTERNAL_ERROR", "An internal error occurred.")

    return app


app = create_app()
