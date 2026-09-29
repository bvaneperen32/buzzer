import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


class BuzzerError(Exception):
    """Base application error."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(BuzzerError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__("NOT_FOUND", message, status.HTTP_404_NOT_FOUND)


class ForbiddenError(BuzzerError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__("FORBIDDEN", message, status.HTTP_403_FORBIDDEN)


class UnauthorizedError(BuzzerError):
    def __init__(self, message: str = "Unauthorized"):
        super().__init__("UNAUTHORIZED", message, status.HTTP_401_UNAUTHORIZED)


class ConflictError(BuzzerError):
    def __init__(self, message: str = "Conflict"):
        super().__init__("CONFLICT", message, status.HTTP_409_CONFLICT)


class UnprocessableError(BuzzerError):
    """A 422 with the standard {"error", "message"} body — unlike FastAPI's request
    validation 422, whose `detail` is an array."""

    def __init__(self, message: str = "Unprocessable"):
        super().__init__("UNPROCESSABLE", message, status.HTTP_422_UNPROCESSABLE_ENTITY)


class PayloadTooLargeError(BuzzerError):
    """A 413 raised by the application's own size checks. nginx's per-route body limits
    produce their own 413 page before a request reaches the backend."""

    def __init__(self, message: str = "The file is too large"):
        super().__init__(
            "PAYLOAD_TOO_LARGE", message, status.HTTP_413_CONTENT_TOO_LARGE
        )


class UnsupportedMediaTypeError(BuzzerError):
    def __init__(self, message: str = "Unsupported media type"):
        super().__init__(
            "UNSUPPORTED_MEDIA_TYPE", message, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        )


class ServiceUnavailableError(BuzzerError):
    """A 503 for work refused because capacity is exhausted (image processing slots)."""

    def __init__(self, message: str = "Service unavailable"):
        super().__init__(
            "SERVICE_UNAVAILABLE", message, status.HTTP_503_SERVICE_UNAVAILABLE
        )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BuzzerError)
    async def buzzer_error_handler(request: Request, exc: BuzzerError) -> JSONResponse:
        logger.warning(
            "app_error", code=exc.code, message=exc.message, path=str(request.url)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("validation_error", errors=exc.errors(), path=str(request.url))
        # exc.errors() can embed non-JSON-serializable objects (e.g. Pydantic v2 puts the
        # raw exception in a field-validator error's `ctx`), so this must go through
        # jsonable_encoder the same way FastAPI's own default handler does -- passing it
        # straight to JSONResponse crashes with a generic 500 that hides the real error.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "VALIDATION_ERROR",
                "detail": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", exc_info=exc, path=str(request.url))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "INTERNAL_ERROR",
                "message": "An unexpected error occurred",
            },
        )
