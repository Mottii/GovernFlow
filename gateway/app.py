from fastapi import FastAPI

app = FastAPI(title="Governed Automation Gateway")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
