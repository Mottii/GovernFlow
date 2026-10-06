# Implementation decisions

- T1.1: `uv` pins Python dependencies. FastAPI and uvicorn serve the native HTTP endpoints; httpx handles asynchronous upstream forwarding; SQLAlchemy, asyncpg, and Alembic manage Postgres; pydantic-settings validates environment configuration; cryptography provides AES-GCM. pytest, pytest-asyncio, and respx test async calls; ruff and mypy run in CI. Each dependency is required by the blueprint's fixed stack.
- T1.1: The administrative API uses a separate ASGI app and port so proxy credentials cannot access its routes.
