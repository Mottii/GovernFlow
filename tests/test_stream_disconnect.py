import asyncio

import httpx
import pytest
import respx
from sqlalchemy import select

from core.models import Budget, Event


class Interrupted(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"part"}}]}\n\n'
        raise httpx.ReadError("connection interrupted")


@pytest.mark.asyncio
async def test_socket_interruption_settles_without_terminal_usage(governed):
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://provider.test/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, stream=Interrupted(), headers={"content-type": "text/event-stream"}
            )
        )
        with pytest.raises(httpx.ReadError):
            await governed.http.post(
                "/v1/chat/completions",
                json={"model": "gpt-mock", "stream": True, "max_tokens": 16},
                headers={"authorization": "Bearer " + governed.a.key},
            )
    await asyncio.sleep(0)
    async with governed.factory() as session:
        event = (await session.execute(select(Event))).scalar_one()
        assert event.estimated and event.cost_micro > 0
        budget = (
            await session.execute(
                select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
            )
        ).scalar_one()
        assert budget.reserved_micro == 0
