"""Domain errors that map onto HTTP responses.

Routers raise these; a single handler registered in ``app.main`` turns them
into a consistent JSON error envelope. Keeping HTTP semantics in the
exception type means the service layer never imports FastAPI.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors that are safe to show to a client."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return {"error": payload}


class EmptyUploadError(AppError):
    status_code = 400
    code = "empty_upload"


class UnsupportedFileError(AppError):
    """The upload is not a format this service knows how to read."""

    status_code = 400
    code = "unsupported_file_type"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class InvalidGeospatialFileError(AppError):
    """Recognised format, but the content could not be parsed."""

    status_code = 422
    code = "invalid_geospatial_file"


class ResourceNotFoundError(AppError):
    status_code = 404
    code = "not_found"


class InvalidParameterError(AppError):
    status_code = 400
    code = "invalid_parameter"
