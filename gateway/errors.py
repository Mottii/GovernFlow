from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException


class GatewayError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, *, retry_after: int | None = None
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def error_response(request: Request, exc: GatewayError) -> JSONResponse:
    headers = {"x-gateway-error": exc.code}
    if exc.retry_after is not None:
        headers["retry-after"] = str(exc.retry_after)
    if request.url.path == "/v1/messages":
        types = {
            401: "authentication_error",
            403: "permission_error",
            404: "not_found_error",
            429: "rate_limit_error",
            529: "overloaded_error",
        }
        content = {
            "type": "error",
            "error": {
                "type": types.get(
                    exc.status_code,
                    "api_error" if exc.status_code >= 500 else "invalid_request_error",
                ),
                "message": exc.message,
            },
        }
    elif request.url.path.startswith(("/v1/models/", "/v1beta/models/")):
        statuses = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RESOURCE_EXHAUSTED",
            502: "UNAVAILABLE",
            503: "UNAVAILABLE",
            504: "DEADLINE_EXCEEDED",
        }
        content = {
            "error": {
                "code": exc.status_code,
                "message": exc.message,
                "status": statuses.get(exc.status_code, "INTERNAL"),
            }
        }
    else:
        content = {
            "error": {
                "message": exc.message,
                "type": "server_error" if exc.status_code >= 500 else "invalid_request_error",
                "param": None,
                "code": exc.code,
            }
        }
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


async def openai_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)
    return error_response(request, exc)


async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DBAPIError)
    return error_response(
        request, GatewayError(503, "governance_unavailable", "Governance storage is unavailable")
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(request, GatewayError(400, "invalid_request", "Request is invalid"))


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    return error_response(
        request, GatewayError(exc.status_code, "invalid_request", str(exc.detail))
    )
