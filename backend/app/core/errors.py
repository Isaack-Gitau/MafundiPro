from collections.abc import Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Represent an expected application-level HTTP error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        super().__init__(message)


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the standard API error response."""
    request_id = getattr(request.state, "request_id", "unknown")

    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
        headers=response_headers,
    )


async def app_error_handler(
    request: Request,
    exc: AppError,
) -> JSONResponse:
    return error_response(
        request,
        exc.status_code,
        exc.code,
        exc.message,
        exc.headers,
    )


async def http_error_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    messages = {
        400: ("BAD_REQUEST", "The request could not be processed."),
        401: ("UNAUTHORIZED", "Authentication is required."),
        403: ("FORBIDDEN", "You are not allowed to perform this action."),
        404: ("NOT_FOUND", "The requested resource was not found."),
        405: ("METHOD_NOT_ALLOWED", "This request method is not supported."),
        409: ("CONFLICT", "The request conflicts with the current resource state."),
        429: ("TOO_MANY_REQUESTS", "Too many requests. Please try again later."),
    }
    code, message = messages.get(
        exc.status_code,
        ("HTTP_ERROR", "The request could not be completed."),
    )

    return error_response(
        request,
        exc.status_code,
        code,
        message,
        exc.headers,
    )


async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return error_response(
        request,
        422,
        "VALIDATION_ERROR",
        "The submitted data is invalid.",
    )


async def unexpected_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    return error_response(
        request,
        500,
        "INTERNAL_SERVER_ERROR",
        "An unexpected server error occurred.",
    )
