"""Application exceptions. Each carries an HTTP status and a stable error code."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class BadRequestError(AppError):
    """Malformed, corrupt, empty or unsafe input (HTTP 400)."""

    status_code = 400
    code = "BAD_REQUEST"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"


class UnprocessableFileError(AppError):
    """The file is structurally valid but its content cannot be used (HTTP 422)."""

    status_code = 422
    code = "UNPROCESSABLE_FILE"


class InvalidParameterError(AppError):
    status_code = 422
    code = "INVALID_PARAMETER"


class StorageError(AppError):
    status_code = 500
    code = "STORAGE_ERROR"


class DriverUnavailableError(AppError):
    status_code = 501
    code = "DRIVER_UNAVAILABLE"
