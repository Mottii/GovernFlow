from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI(title="Mock AI Provider")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body = await request.json()
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
