"""Create the first dashboard administrator from local environment values."""

import asyncio
import os

import httpx
from dotenv import dotenv_values


async def main() -> None:
    env = {**dotenv_values(".env"), **os.environ}
    required = ("ADMIN_TOKEN", "DASHBOARD_ADMIN_EMAIL", "DASHBOARD_ADMIN_PASSWORD")
    if any(not env.get(name) for name in required):
        raise SystemExit(
            "Set ADMIN_TOKEN, DASHBOARD_ADMIN_EMAIL and DASHBOARD_ADMIN_PASSWORD in .env"
        )
    async with httpx.AsyncClient(
        base_url=env.get("ADMIN_API_URL", "http://localhost:8001")
    ) as http:
        response = await http.post(
            "/admin/users",
            headers={"authorization": "Bearer " + str(env["ADMIN_TOKEN"])},
            json={
                "email": env["DASHBOARD_ADMIN_EMAIL"],
                "password": env["DASHBOARD_ADMIN_PASSWORD"],
                "role": "admin",
            },
        )
        if response.status_code == 409:
            print("Administrator already exists; use that account or choose another email")
        else:
            response.raise_for_status()
            print("Dashboard administrator created; sign in with the configured email and password")


if __name__ == "__main__":
    asyncio.run(main())
