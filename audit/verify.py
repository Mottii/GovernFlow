import argparse
import asyncio
import sys
import uuid

from audit.chain import verify_chain
from core.database import session_factory


async def run(client_id: uuid.UUID) -> int:
    async with session_factory() as session:
        valid, bad_id = await verify_chain(session, client_id)
    if valid:
        print(f"Audit chain verified for client {client_id}")
        return 0
    print(f"Audit chain mismatch at event {bad_id}", file=sys.stderr)
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a client's append-only audit chain")
    parser.add_argument("--client", required=True, type=uuid.UUID)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.client)))


if __name__ == "__main__":
    main()
