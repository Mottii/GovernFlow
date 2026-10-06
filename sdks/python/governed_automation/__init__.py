"""Execution wrappers. HMAC verification and consumption happen at the gateway."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

T = TypeVar("T")


class ActionBlocked(RuntimeError):
    pass


class Gateway:
    def __init__(self, base_url: str, virtual_key: str, *, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30)
        self.headers = {"authorization": "Bearer " + virtual_key}
        self.owns_client = client is None

    async def check_action(
        self,
        action_name: str,
        action: dict[str, Any],
        summary: str,
        *,
        wait_timeout: float = 0,
        poll_interval: float = 1,
    ) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/actions/check",
            headers=self.headers,
            json={"action_name": action_name, "action": action, "summary": summary},
        )
        response.raise_for_status()
        result = response.json()
        deadline = time.monotonic() + wait_timeout
        while result["decision"] == "pending" and time.monotonic() < deadline:
            await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            response = await self.client.get(
                "/v1/actions/" + result["approval_id"], headers=self.headers
            )
            response.raise_for_status()
            result = response.json()
        return dict(result)

    async def verify_token(self, token: str, action_name: str, action: dict[str, Any]) -> bool:
        response = await self.client.post(
            "/v1/actions/consume",
            headers=self.headers,
            json={"token": token, "action_name": action_name, "action": action},
        )
        response.raise_for_status()
        if not response.json().get("consumed"):
            raise ActionBlocked("Approval was not consumed")
        return True

    async def execute(
        self,
        action_name: str,
        action: dict[str, Any],
        summary: str,
        function: Callable[[], Awaitable[T]],
        *,
        wait_timeout: float = 900,
    ) -> T:
        result = await self.check_action(action_name, action, summary, wait_timeout=wait_timeout)
        if result["decision"] != "allow" or not result.get("token"):
            raise ActionBlocked("Action is " + result["decision"])
        await self.verify_token(result["token"], action_name, action)
        return await function()

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()
