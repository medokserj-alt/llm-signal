from fastapi import APIRouter
from app.agent.evaluator import evaluate_signals

router = APIRouter()

@router.post("/eval_signals")
def eval_signals():
    """
    Запуск оценки сигналов.
    Пока источник цен — заглушка, но структура результата реальная.
    """
    result = evaluate_signals(window_minutes=1)
    return {"ok": True, "result": result}
