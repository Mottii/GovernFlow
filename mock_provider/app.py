from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

app = FastAPI(title="Mock AI Provider")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body = await request.json()
    if body.get("stream"):

        async def chunks() -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"mock reply"}}],"usage":null}\n\n'
            if body.get("stream_options", {}).get("include_usage"):
                yield (
                    b'data: {"choices":[],"usage":{"prompt_tokens":12,'
                    b'"completion_tokens":3,"total_tokens":15}}\n\n'
                )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")
    return Response(
        content=(
            '{"id":"chatcmpl_mock","object":"chat.completion","model":"'
            + body["model"]
            + '","choices":[{"index":0,"message":{"role":"assistant","content":"mock reply"},'
            '"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":3,'
            '"total_tokens":15}}'
        ),
        media_type="application/json",
    )
