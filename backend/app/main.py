from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.errors import (
    AppError,
    app_error_handler,
    http_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from app.core.middleware import RequestIDMiddleware

app = FastAPI(
    title=settings.app_name,
    description=(
        "A marketplace connecting skilled workers with people "
        "and businesses seeking their services."
    ),
    version="0.1.0",
    debug=settings.app_debug,
)

app.add_middleware(RequestIDMiddleware)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unexpected_error_handler)

app.include_router(health_router)
app.include_router(v1_router, prefix=settings.api_v1_prefix)


@app.get("/", tags=["Root"])
def root() -> dict[str, str]:
    """Provide basic API information."""
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "status": "running",
        "documentation": "/docs",
    }
