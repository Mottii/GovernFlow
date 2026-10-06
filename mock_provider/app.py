import json
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


@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Response:
    body = await request.json()
    message = {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": [{"type": "text", "text": "mock reply"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    if body.get("stream"):

        async def chunks() -> AsyncIterator[bytes]:
            start = {
                **message,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 12, "output_tokens": 1},
            }
            events = [
                ("message_start", {"type": "message_start", "message": start}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "mock reply"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 3},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            for name, data in events:
                yield f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

        return StreamingResponse(chunks(), media_type="text/event-stream")
    return Response(json.dumps(message, separators=(",", ":")), media_type="application/json")


@app.post("/v1/responses")
async def openai_responses(request: Request) -> Response:
    body = await request.json()
    response = {
        "id": "resp_mock",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": body["model"],
        "output": [
            {
                "id": "msg_mock",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "mock reply", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 15,
        },
    }
    if body.get("stream"):

        async def chunks() -> AsyncIterator[bytes]:
            events = [
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            **response,
                            "status": "in_progress",
                            "output": [],
                            "usage": None,
                        },
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "mock reply",
                    },
                ),
                ("response.completed", {"type": "response.completed", "response": response}),
            ]
            for name, data in events:
                yield f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

        return StreamingResponse(chunks(), media_type="text/event-stream")
    return Response(json.dumps(response, separators=(",", ":")), media_type="application/json")


@app.post("/v1/embeddings")
async def openai_embeddings(request: Request) -> Response:
    body = await request.json()
    response = {
        "object": "list",
        "data": [{"object": "embedding", "embedding": [0.1, 0.2, 0.3], "index": 0}],
        "model": body["model"],
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    }
    return Response(json.dumps(response, separators=(",", ":")), media_type="application/json")
