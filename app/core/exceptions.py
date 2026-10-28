"""Application error hierarchy, rendered as RFC 7807 `application/problem+json`."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import request_id_var
from app.core.logging import get_logger

log = get_logger(__name__)

ERROR_TYPE_BASE = "https://errors.masteratlas.ai/"


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        title: str | None = None,
        status_code: int | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.code = code or self.code
        self.title = title or self.title
        self.status_code = status_code or self.status_code
        self.errors = errors
        self.headers = headers


class Unauthenticated(AppError):
    status_code, code, title = 401, "unauthenticated", "Authentication required"


class Forbidden(AppError):
    status_code, code, title = 403, "forbidden", "Permission denied"


class NotFound(AppError):
    status_code, code, title = 404, "not_found", "Resource not found"


class Conflict(AppError):
    status_code, code, title = 409, "conflict", "Conflict"


class VersionConflict(Conflict):
    code, title = "version_conflict", "The resource was modified by someone else"


class Locked(AppError):
    status_code, code, title = 423, "account_locked", "Account temporarily locked"


class ValidationFailed(AppError):
    status_code, code, title = 422, "validation_error", "Validation failed"


class RateLimited(AppError):
    status_code, code, title = 429, "rate_limited", "Too many requests"


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None,
    instance: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": ERROR_TYPE_BASE + code,
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": instance,
        "request_id": request_id_var.get(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", code=exc.code, detail=exc.detail)
        return problem_response(
            status=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            instance=request.url.path,
            errors=exc.errors,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"]}
            for err in exc.errors()
        ]
        return problem_response(
            status=422,
            code="validation_error",
            title="Validation failed",
            detail=f"{len(errors)} field(s) are invalid",
            instance=request.url.path,
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return problem_response(
            status=exc.status_code,
            code=code,
            title=str(exc.detail),
            detail=None,
            instance=request.url.path,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return problem_response(
            status=500,
            code="internal_error",
            title="Internal server error",
            detail=None,
            instance=request.url.path,
        )
