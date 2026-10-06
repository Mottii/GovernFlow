import json

import httpx
import pytest
import respx
from sqlalchemy import select, update

from core.models import Budget, KillFlag

ROUTES = [
    ("/v1/chat/completions", "openai", "gpt-mock", {"max_tokens": 16}),
    ("/v1/responses", "openai", "gpt-mock", {"max_output_tokens": 16}),
    ("/v1/embeddings", "openai", "text-embedding-mock", {}),
    ("/v1/messages", "anthropic", "claude-mock", {"max_tokens": 16}),
    (
        "/v1beta/models/gemini-mock:generateContent",
        "gemini",
        "gemini-mock",
        {"generationConfig": {"maxOutputTokens": 16}},
    ),
]


def headers(provider, key):
    return (
        {"authorization": "Bearer " + key}
        if provider == "openai"
        else {"x-api-key" if provider == "anthropic" else "x-goog-api-key": key}
    )


def native(response, provider, status):
    assert response.status_code == status
    payload = response.json()
    assert "detail" not in payload
    if provider == "anthropic":
        assert payload["type"] == "error"
        assert "type" in payload["error"]
    elif provider == "gemini":
        assert payload["error"]["code"] == status
        assert "status" in payload["error"]
    else:
        assert {"type", "code", "param", "message"} <= payload["error"].keys()
    assert "provider-A" not in response.text


@pytest.mark.asyncio
async def test_error_matrix(governed):
    for path, provider, model, extra in ROUTES:
        body = {"model": model, **extra}
        auth = headers(provider, governed.a.key)
        native(await governed.http.post(path, json=body), provider, 401)
        native(await governed.http.post(path, content=b"{bad", headers=auth), provider, 400)
        async with governed.factory() as session:
            await session.execute(
                update(Budget)
                .where(Budget.workflow_id == governed.a.workflow.id)
                .values(cap_micro=0)
            )
            await session.commit()
        native(await governed.http.post(path, json=body, headers=auth), provider, 429)
        async with governed.factory() as session:
            await session.execute(update(Budget).values(cap_micro=100000))
            session.add(
                KillFlag(
                    scope="workflow",
                    scope_id=str(governed.a.workflow.id),
                    active=True,
                    reason="operator stop",
                )
            )
            await session.commit()
        native(await governed.http.post(path, json=body, headers=auth), provider, 503)
        async with governed.factory() as session:
            flag = await session.get(KillFlag, ("workflow", str(governed.a.workflow.id)))
            await session.delete(flag)
            await session.commit()
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://provider.test" + path).mock(side_effect=httpx.ReadTimeout("timeout"))
            native(await governed.http.post(path, json=body, headers=auth), provider, 504)
        payload = json.dumps({"error": {"native": "provider rejection"}}).encode()
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://provider.test" + path).respond(
                429, content=payload, headers={"retry-after": "17"}
            )
            response = await governed.http.post(path, json=body, headers=auth)
            assert response.status_code == 429 and response.content == payload
            assert response.headers["retry-after"] == "17"
    async with governed.factory() as session:
        budgets = (await session.execute(select(Budget))).scalars().all()
        assert all(b.spent_micro == 0 and b.reserved_micro == 0 for b in budgets)


@pytest.mark.asyncio
async def test_storage_outage_fails_closed_in_each_dialect(governed, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import gateway.auth
    import gateway.gemini

    async def unavailable(*args, **kwargs):
        raise OperationalError("connect", {}, Exception("private database failure"))

    monkeypatch.setattr(gateway.auth, "resolve_virtual_key", unavailable)
    monkeypatch.setattr(gateway.gemini, "resolve_virtual_key", unavailable)
    for path, provider, model, extra in ROUTES:
        response = await governed.http.post(
            path, json={"model": model, **extra}, headers=headers(provider, governed.a.key)
        )
        native(response, provider, 503)
        assert "private" not in response.text
