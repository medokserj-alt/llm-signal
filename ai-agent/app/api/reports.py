from fastapi import APIRouter, HTTPException, Query

from app.agent.reports import build_trade_report, build_daily_summary

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


@router.get("/daily_summary")
def daily_summary(window_hours: int = Query(24, ge=1, le=168, description="Горизонт анализа в часах")):
    """
    Сводка по всем trade_result за последние window_hours часов.
    """
    summary = build_daily_summary(window_hours=window_hours)
    return summary
