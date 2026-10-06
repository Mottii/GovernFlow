"""Read chat IDs or register a webhook using environment secrets; never print the token."""

import argparse
import asyncio
import os

import httpx
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["chat-ids", "set-webhook"])
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first")
    method = "getUpdates" if args.command == "chat-ids" else "setWebhook"
    payload = {}
    if method == "setWebhook":
        public = os.environ.get("PUBLIC_ADMIN_URL", "")
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if not public.startswith("https://") or not secret:
            raise SystemExit("Set HTTPS PUBLIC_ADMIN_URL and TELEGRAM_WEBHOOK_SECRET first")
        payload = {
            "url": public.rstrip("/") + "/integrations/telegram",
            "secret_token": secret,
            "allowed_updates": ["callback_query"],
        }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/{method}", json=payload
            )
            result = response.json()
    except httpx.HTTPError:
        raise SystemExit("Telegram request failed; check connectivity and token") from None
    if not result.get("ok"):
        raise SystemExit("Telegram rejected the request; getUpdates requires no active webhook")
    if method == "getUpdates":
        ids = {
            str(update["message"]["chat"]["id"])
            for update in result["result"]
            if "message" in update and update["message"]["chat"].get("type") == "private"
        }
        print("Private approver chat IDs: " + ",".join(sorted(ids)))
    else:
        print("Webhook registered")


if __name__ == "__main__":
    asyncio.run(main())
