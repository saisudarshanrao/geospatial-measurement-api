"""Application factory, middleware and exception handling.

``create_app`` exists rather than a module-level ``app`` so tests can build an
isolated instance with its own settings and temporary storage. A module-level
``app`` is provided too, since ``uvicorn app.main:app`` is the obvious way to
run it.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app import __version__
from app.api.routes_files import router as files_router
from app.config import Settings, get_settings
from app.errors import AppError
from app.geo.crs import MeasurementStrategy
from app.geo.detect import ACCEPTED_EXTENSIONS
from app.geo.measure import SUPPORTED_GEOMETRY_TYPES
from app.logging_config import configure_logging
from app.schemas import HealthResponse
from app.services.processing import FileProcessor
from app.services.storage import InMemoryFileRepository, UploadStore

logger = logging.getLogger(__name__)

DESCRIPTION = """
Upload a geospatial file and get measurements back.

**Accepted uploads**

* `.zip` containing a shapefile (`.shp` + `.shx` + `.dbf`, ideally with `.prj`)
* `.kml`
* `.kmz` (zipped KML)

**Measurements**

| Geometry | Reported |
| --- | --- |
| Polygon, MultiPolygon | area (and perimeter) |
| LineString, MultiLineString | length |
| Point, MultiPoint | nothing - no measurement is defined |

**CRS handling**

Area and length are never computed from longitude/latitude degrees. Geographic
input is reprojected first: into the UTM zone containing the feature, or, for
features too wide for one zone, into an azimuthal projection centred on the
feature. Files that are already projected are measured in their own CRS, with
their axis unit converted to metres. Pass `?strategy=geodesic` to measure on
the WGS 84 ellipsoid instead of projecting.
""".strip()

TAGS_METADATA = [
    {"name": "files", "description": "Upload geospatial files and read their measurements."},
    {"name": "health", "description": "Service metadata and liveness."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Geospatial File Measurement API",
        description=DESCRIPTION,
        version=__version__,
        openapi_tags=TAGS_METADATA,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.state.settings = settings
    app.state.repository = InMemoryFileRepository()
    app.state.upload_store = UploadStore(settings.storage_dir, enabled=settings.keep_uploads)
    app.state.processor = FileProcessor(app.state.repository, app.state.upload_store, settings)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    _register_middleware(app)
    _register_exception_handlers(app)

    app.include_router(files_router)

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["health"],
        summary="Liveness and capabilities",
    )
    def health() -> HealthResponse:
        """Liveness probe that also advertises what the service can read and measure."""
        return HealthResponse(
            status="ok",
            version=__version__,
            supported_uploads=list(ACCEPTED_EXTENSIONS),
            supported_geometry_types=SUPPORTED_GEOMETRY_TYPES,
            measurement_strategies=[strategy.value for strategy in MeasurementStrategy],
        )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Tag every request with an id and log how long it took."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.info(
            "client_error",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "code": exc.code,
                "status_code": exc.status_code,
            },
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Re-shape FastAPI's validation errors into this API's error envelope."""
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "details": _safe_validation_details(exc),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Nothing internal leaks to the client; the detail is in the log, keyed
        # by the request id that the client also receives.
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled_error", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred while handling the request.",
                    "details": {"request_id": request_id},
                }
            },
        )


def _safe_validation_details(exc: RequestValidationError) -> list[dict[str, object]]:
    """Validation errors without the echoed input, which can be large or binary."""
    details = []
    for error in exc.errors():
        details.append(
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg"),
                "type": error.get("type"),
            }
        )
    return details


app = create_app()
