import asyncio
import json

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database import get_session
from core.models import Budget, ProviderCall, Workflow
from gateway.app import app
from tests.test_native_errors import ROUTES, headers


@pytest.mark.parametrize(
    "error,state",
    [
        (httpx.ConnectTimeout("before send"), "voided"),
        (httpx.ReadTimeout("ambiguous"), "settled_estimated"),
        (httpx.WriteError("partial write"), "settled_estimated"),
    ],
)
async def test_transport_failures_only_void_proven_non_dispatch(governed, error, state):
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://provider.test/v1/chat/completions").mock(side_effect=error)
        response = await governed.http.post(
            "/v1/chat/completions",
            json={"model": "gpt-mock", "max_tokens": 16},
            headers={"authorization": "Bearer " + governed.a.key},
        )
        assert response.status_code in (502, 504)
        assert response.headers["x-gateway-call-id"]
        assert route.call_count == 1
    async with governed.factory() as session:
        call = (await session.execute(select(ProviderCall))).scalar_one()
        assert call.state == state
        budget = await session.get(Budget, call.budget_id)
        assert budget.reserved_micro == 0
        assert (budget.spent_micro == 0) == (state == "voided")


async def test_output_limits_are_injected_in_every_native_dialect_and_explicit_values_preserved(
    governed,
):
    for path, provider, model, extra in ROUTES:
        response_payload = {"usage": {"prompt_tokens": 2, "completion_tokens": 1}}
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("http://provider.test" + path).respond(200, json=response_payload)
            response = await governed.http.post(
                path, json={"model": model}, headers=headers(provider, governed.a.key)
            )
            assert response.status_code == 200, response.text
            forwarded = json.loads(route.calls[0].request.content)
            if provider == "gemini":
                assert forwarded["generationConfig"]["maxOutputTokens"] == 4096
            elif path == "/v1/chat/completions":
                assert forwarded["max_completion_tokens"] == 4096
            elif path == "/v1/responses":
                assert forwarded["max_output_tokens"] == 4096
            elif path == "/v1/messages":
                assert forwarded["max_tokens"] == 4096
            else:
                assert forwarded == {"model": model}
        body = json.dumps({"model": model, **extra}, separators=(",", ":")).encode()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("http://provider.test" + path).respond(200, json=response_payload)
            assert (
                await governed.http.post(
                    path, content=body, headers=headers(provider, governed.a.key)
                )
            ).status_code == 200
            assert route.calls[0].request.content == body


async def test_body_limits_enforce_content_length_and_chunked_uploads(governed, monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BYTES", "64")
    response = await governed.http.post("/v1/chat/completions", content=b"x" * 65)
    assert response.status_code == 413

    async def chunks():
        yield b'{"model":"gpt-mock","input":"'
        yield b"x" * 65
        yield b'"}'

    response = await governed.http.post(
        "/v1/chat/completions",
        content=chunks(),
        headers={"authorization": "Bearer " + governed.a.key},
    )
    assert response.status_code == 413, response.text


async def test_one_hundred_slow_streams_release_all_five_database_connections(governed):
    import os

    engine = create_async_engine(
        os.environ["APP_TEST_DATABASE_URL"], pool_size=5, max_overflow=0, pool_timeout=5
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session_dependency():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    service = app.state.gateway_runtime
    service.workflow_limit = 128
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(
                policy={
                    "models": ["*"],
                    "rpm": 1000,
                    "loop_breaker": {"rpm": 1000, "max_steps": 200},
                }
            )
        )
        await session.commit()
    all_started, release = asyncio.Event(), asyncio.Event()
    started = 0

    async def provider(request):
        nonlocal started
        started += 1
        if started == 100:
            all_started.set()
        await release.wait()
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
                b"data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    tasks = []
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("http://provider.test/v1/chat/completions").mock(side_effect=provider)
            for i in range(100):
                tasks.append(
                    asyncio.create_task(
                        governed.http.post(
                            "/v1/chat/completions",
                            json={"model": "gpt-mock", "max_tokens": 16, "stream": True},
                            headers={
                                "authorization": "Bearer " + governed.a.key,
                                "x-run-id": f"slow-{i}",
                            },
                        )
                    )
                )
            async with asyncio.timeout(25):
                await all_started.wait()
            assert engine.pool.checkedout() == 0
            assert service.active == 100
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 25)
            assert all(r.status_code == 200 for r in responses)
            assert route.call_count == 100
            assert service.active == 0
        async with governed.factory() as session:
            calls = (await session.execute(select(ProviderCall))).scalars().all()
            assert len(calls) == 100 and all(c.state == "settled_measured" for c in calls)
            budget = (
                await session.execute(
                    select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
                )
            ).scalar_one()
            assert (budget.spent_micro, budget.reserved_micro) == (300, 0)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_backpressure_drain_and_total_deadline_do_not_start_extra_provider_calls(
    governed, monkeypatch
):
    service = app.state.gateway_runtime
    service.workflow_limit = 1
    started, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    auth = {"authorization": "Bearer " + governed.a.key}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://provider.test/v1/chat/completions").mock(side_effect=provider)
        first = asyncio.create_task(
            governed.http.post(
                "/v1/chat/completions", json={"model": "gpt-mock", "max_tokens": 16}, headers=auth
            )
        )
        await started.wait()
        excess = await governed.http.post(
            "/v1/chat/completions", json={"model": "gpt-mock", "max_tokens": 16}, headers=auth
        )
        assert excess.status_code == 429 and excess.headers["retry-after"] == "1"
        service.draining = True
        drained = await governed.http.post(
            "/v1/chat/completions", json={"model": "gpt-mock"}, headers=auth
        )
        assert drained.status_code == 503
        service.draining = False
        release.set()
        assert (await first).status_code == 200
        assert route.call_count == 1
    monkeypatch.setenv("CALL_DEADLINE_SECONDS", "1")

    stalled_started = False

    async def stalled(request):
        nonlocal stalled_started
        stalled_started = True
        await asyncio.sleep(2)
        raise AssertionError("deadline did not cancel provider wait")

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://provider.test/v1/chat/completions").mock(side_effect=stalled)
        response = await governed.http.post(
            "/v1/chat/completions", json={"model": "gpt-mock", "max_tokens": 16}, headers=auth
        )
        assert response.status_code == 504
        assert stalled_started
    async with governed.factory() as session:
        assert (
            await session.execute(
                select(ProviderCall).where(ProviderCall.state == "settled_estimated")
            )
        ).scalar_one().charged_micro > 0


async def test_client_cancellation_after_dispatch_leaves_a_durable_estimate(governed):
    started = asyncio.Event()

    async def provider(request):
        started.set()
        await asyncio.Event().wait()

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://provider.test/v1/chat/completions").mock(side_effect=provider)
        task = asyncio.create_task(
            governed.http.post(
                "/v1/chat/completions",
                json={"model": "gpt-mock", "max_tokens": 16},
                headers={"authorization": "Bearer " + governed.a.key},
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with governed.factory() as session:
        call = (await session.execute(select(ProviderCall))).scalar_one()
        assert call.state == "settled_estimated" and call.charged_micro > 0
        budget = await session.get(Budget, call.budget_id)
        assert budget.spent_micro == call.charged_micro and budget.reserved_micro == 0


async def test_initial_run_cap_rejection_does_not_reference_a_rolled_back_run(governed):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(policy={"models": ["*"], "per_run_micro": 1})
        )
        await session.commit()
    response = await governed.http.post(
        "/v1/chat/completions",
        json={"model": "gpt-mock", "max_tokens": 16},
        headers={"authorization": "Bearer " + governed.a.key},
    )
    assert response.status_code == 429, response.text
    async with governed.factory() as session:
        assert not (await session.execute(select(ProviderCall))).scalars().all()
        budget = (
            await session.execute(
                select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
            )
        ).scalar_one()
        assert budget.reserved_micro == 0 and budget.spent_micro == 0
