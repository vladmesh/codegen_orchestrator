from fastapi import FastAPI
from .config import settings
import structlog

logger = structlog.get_logger()

app = FastAPI(title="Worker Manager")


@app.get("/health")
async def health_check():
    return {"status": "ok", "environment": settings.ENVIRONMENT}
