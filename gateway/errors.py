from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


async def openai_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)
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
