from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


async def openai_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)
    if request.url.path == "/v1/messages":
        if exc.status_code == 401:
            error_type = "authentication_error"
        elif exc.status_code == 429:
            error_type = "rate_limit_error"
        elif exc.status_code >= 500:
            error_type = "api_error"
        else:
            error_type = "invalid_request_error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": "error", "error": {"type": error_type, "message": exc.message}},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": "invalid_request_error" if exc.status_code < 500 else "server_error",
                "param": None,
                "code": exc.code,
            }
        },
    )
