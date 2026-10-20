"""Application-lifetime transport, admission control, and bounded request bodies."""

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.errors import GatewayError, error_response


def positive_setting(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise RuntimeError(name + " must be positive")
    return value


class Runtime:
    def __init__(self, store: Any) -> None:
        self.limit = positive_setting("GATEWAY_MAX_INFLIGHT", 128)
        self.workflow_limit = positive_setting("WORKFLOW_MAX_INFLIGHT", 32)
        self.active = 0
        self.workflows: Counter[uuid.UUID] = Counter()
        self.draining = False
        self.journal = store
        self.failed_evidence: set[uuid.UUID] = set()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(os.environ.get("UPSTREAM_TIMEOUT_S", "120")), connect=10, pool=2
            ),
            limits=httpx.Limits(max_connections=self.limit, max_keepalive_connections=64),
            follow_redirects=False,
        )

    def workflow(self, identity: uuid.UUID) -> None:
        if self.workflows[identity] >= self.workflow_limit:
            raise GatewayError(
                429, "workflow_busy", "Workflow concurrency limit reached", retry_after=1
            )
        self.workflows[identity] += 1

    def track(self, task: asyncio.Task[Any]) -> None:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def preserve(self, record: dict[str, Any]) -> None:
        from policy.journal import persist

        identity = uuid.UUID(record["evidence_id"])
        try:
            await persist(record, store=self.journal)
        except (OSError, RuntimeError):
            self.failed_evidence.add(identity)

            async def retry() -> None:
                delay = 1
                while True:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    try:
                        await persist(record, store=self.journal)
                        self.failed_evidence.discard(identity)
                        return
                    except (OSError, RuntimeError):
                        continue

            self.track(asyncio.create_task(retry()))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from policy.journal import journal

    mode = os.environ.get("DEPLOYMENT_MODE", "self_hosted")
    if mode not in ("self_hosted", "hosted"):
        raise RuntimeError("Unknown deployment mode")
    for name, default in (
        ("MAX_OUTPUT_TOKENS", 32768),
        ("DEFAULT_MAX_OUTPUT_TOKENS", 4096),
        ("MAX_REQUEST_BYTES", 2097152),
        ("MAX_RESPONSE_BYTES", 16777216),
        ("MAX_TRACKER_BYTES", 2097152),
        ("CALL_DEADLINE_SECONDS", 300),
    ):
        positive_setting(name, default)
    if positive_setting("CALL_RENEW_SECONDS", 15) >= positive_setting("CALL_LEASE_SECONDS", 60):
        raise RuntimeError("Call renewal interval must be shorter than its lease")
    if os.environ.get("ALLOW_FAIL_OPEN", "false").lower() == "true" and (
        mode != "self_hosted"
        or os.environ.get("SELF_HOSTED_SINGLE_NODE", "false").lower() != "true"
        or os.environ.get("RECOVERY_BACKEND", "sqlite") != "sqlite"
    ):
        raise RuntimeError(
            "Fail open requires explicit single-node self-hosting and persistent SQLite"
        )
    store = journal()
    from core.database import agency_rls_enabled
    from policy.journal import SQLiteJournal

    if (
        isinstance(store, SQLiteJournal)
        and agency_rls_enabled()
        and str(store.path).startswith(("/tmp/", "/private/tmp/"))
    ):
        raise RuntimeError("Runtime SQLite recovery requires a persistent storage path")
    await store.probe()
    runtime = Runtime(store)
    app.state.gateway_runtime = runtime
    try:
        yield
    finally:
        runtime.draining = True
        if runtime.tasks:
            _, pending = await asyncio.wait(runtime.tasks, timeout=30)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await runtime.http.aclose()
        from policy.journal import S3Journal

        if isinstance(store, S3Journal):
            await asyncio.to_thread(store.client.close)


def runtime(request: Request) -> Runtime:
    result: Runtime = request.app.state.gateway_runtime
    return result


class LimitsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        service: Runtime = scope["app"].state.gateway_runtime
        governed = scope["method"] == "POST" and (
            scope["path"]
            in ("/v1/chat/completions", "/v1/responses", "/v1/embeddings", "/v1/messages")
            or scope["path"].endswith((":generateContent", ":streamGenerateContent"))
        )
        maximum = positive_setting("MAX_REQUEST_BYTES", 2 * 1024 * 1024)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            length = maximum + 1
        if length < 0 or length > maximum:
            await error_response(
                request, GatewayError(413, "request_too_large", "Request body exceeds limit")
            )(scope, receive, send)
            return
        if governed and (
            service.draining or service.failed_evidence or service.active >= service.limit
        ):
            unavailable = service.draining or service.failed_evidence
            await error_response(
                request,
                GatewayError(
                    503 if unavailable else 429,
                    "gateway_unavailable" if unavailable else "gateway_busy",
                    "Gateway is draining or recovery storage is unavailable"
                    if unavailable
                    else "Gateway concurrency limit reached",
                    retry_after=1,
                ),
            )(scope, receive, send)
            return
        size = 0

        async def bounded_receive() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > maximum:
                    raise GatewayError(413, "request_too_large", "Request body exceeds limit")
            return message

        if governed:
            service.active += 1
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            if governed:
                service.active -= 1
                identity = scope.get("state", {}).get("admitted_workflow")
                if identity:
                    service.workflows[identity] -= 1
                    if not service.workflows[identity]:
                        del service.workflows[identity]
