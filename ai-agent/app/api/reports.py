from fastapi import APIRouter, HTTPException, Query

from app.agent.reports import build_trade_report

router = APIRouter()


@router.get("/trade_report")
def trade_report(signal_id: str = Query(..., description="ID сигнала")):
    """
    Возвращает структурированный и текстовый отчёт по сделке по signal_id.
    """
    report = build_trade_report(signal_id)
    if not report.get("ok"):
        raise HTTPException(status_code=404, detail=report.get("reason", "not_found"))
    return report
