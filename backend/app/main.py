from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

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


def create_app() -> FastAPI:
    """Create the MafundiPro API application with its security middleware."""
    application = FastAPI(
        title=settings.app_name,
        description=(
            "A marketplace connecting skilled workers with people "
            "and businesses seeking their services."
        ),
        version="0.1.0",
        debug=settings.app_debug,
    )

    application.add_middleware(RequestIDMiddleware)

    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(
        StarletteHTTPException,
        http_error_handler,
    )
    application.add_exception_handler(
        RequestValidationError,
        validation_error_handler,
    )
    application.add_exception_handler(
        Exception,
        unexpected_error_handler,
    )

    application.include_router(health_router)
    application.include_router(
        v1_router,
        prefix=settings.api_v1_prefix,
    )

    # Add CORS last so it wraps the application and handles preflight requests.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_frontend_origins),
        allow_credentials=True,
        allow_methods=[
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-CSRF-Token",
        ],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    @application.get("/", tags=["Root"])
    def root() -> dict[str, str]:
        """Provide basic API information."""
        return {
            "name": settings.app_name,
            "version": "0.1.0",
            "status": "running",
            "documentation": "/docs",
        }

    return application


app = create_app()
