from fastapi import FastAPI

from app.api.telegram import router as telegram_router
from app.api.jobs import router as jobs_router
from app.api.reports import router as reports_router
from app.core.config import settings
from app.core.logging import setup_logging


setup_logging()
app = FastAPI(title=settings.app_name)


@app.get("/health")
def health_check():
    return {"status": "ok"}


app.include_router(telegram_router, prefix="/tg", tags=["telegram"])
app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
app.include_router(reports_router, prefix="/jobs", tags=["reports"])
