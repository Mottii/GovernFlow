from fastapi import FastAPI

app = FastAPI(title="Mock AI Provider")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
