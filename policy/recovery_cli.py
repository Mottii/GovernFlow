"""Inspect scoped recovery jobs or request an accounting retry; never execute a provider call."""

import argparse
import asyncio
import json
import os
import uuid

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("list", "retry", "local-status"))
    parser.add_argument("evidence_id", nargs="?", type=uuid.UUID)
    parser.add_argument("--url", default=os.environ.get("ADMIN_API_URL", "http://localhost:8001"))
    args = parser.parse_args()
    if args.operation == "local-status":
        from policy.journal import SQLiteJournal

        with SQLiteJournal().connection() as connection:
            for identity, status, attempts, error in connection.execute(
                "SELECT id,status,attempts,error FROM evidence ORDER BY id"
            ):
                print(
                    json.dumps(
                        {
                            "evidence_id": identity,
                            "status": status,
                            "attempts": attempts,
                            "last_error": error,
                        }
                    )
                )
        return
    token = os.environ.get("GOVERNFLOW_ADMIN_SESSION", "")
    if not token:
        parser.error("Set GOVERNFLOW_ADMIN_SESSION to an authorized session token")
    if args.operation == "retry" and args.evidence_id is None:
        parser.error("retry requires an evidence UUID")
    async with httpx.AsyncClient(
        base_url=args.url, headers={"authorization": "Bearer " + token}, timeout=15
    ) as client:
        response = (
            await client.get("/admin/accounting-recovery")
            if args.operation == "list"
            else await client.post(f"/admin/accounting-recovery/{args.evidence_id}/retry")
        )
        if not response.is_success:
            raise SystemExit(f"Recovery API returned {response.status_code}")
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
