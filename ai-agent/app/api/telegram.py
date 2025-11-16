from fastapi import APIRouter, Request, HTTPException
from app.agent.graph import run_agent
from app.agent.tools import journal_event

router = APIRouter()


@router.post("/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    agent_response = run_agent(body)
    return {"ok": True, "agent": agent_response}


@router.post("/feedback")
async def telegram_feedback(request: Request):
    body = await request.json()
    journal_event(body, kind="feedback")
    return {"ok": True, "logged": True}


@router.post("/signal")
async def telegram_signal(request: Request):
    body = await request.json()

    # Минимальная sanity-check в стиле V1
    required = ["signal_id", "symbol", "direction", "entry_zone", "sl", "tp", "published_at"]
    for k in required:
        if k not in body:
            raise HTTPException(status_code=400, detail=f"Missing field: {k}")

    journal_event(body, kind="signal")
    return {"ok": True, "logged": True}
