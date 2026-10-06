from fastapi import FastAPI

from gateway.errors import GatewayError, openai_error_handler

app = FastAPI(title="Governed Automation Gateway")
app.add_exception_handler(GatewayError, openai_error_handler)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
