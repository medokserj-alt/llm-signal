#!/usr/bin/env python3
import json
from datetime import datetime
from pathlib import Path

from postprocess import process as pp_process
from get_signal_json import get_ema20_m15, get_ema20_h1, apply_ema_exhale_filter

BASE = Path(__file__).resolve().parent

def _drop_time_window_mentions(data: dict) -> dict:
    def has_tw(s: str) -> bool:
        return "time_window" in s

    def walk(x):
        if isinstance(x, dict):
            for k in list(x.keys()):
                v = x[k]
                if k == "no_trade_reasons" and isinstance(v, list):
                    x[k] = [it for it in v if not (isinstance(it, str) and has_tw(it))]
                    continue
                if k == "warnings" and isinstance(v, list):
                    x[k] = [it for it in v if not (isinstance(it, str) and has_tw(it))]
                    continue
                if k in ("no_trade_hint", "comments") and isinstance(v, str) and has_tw(v):
                    x[k] = ""
                    continue
                if k == "comment" and isinstance(v, str) and has_tw(v):
                    x[k] = ""
                    continue
                walk(v)
        elif isinstance(x, list):
            for it in x:
                walk(it)

    try:
        walk(data)
    except Exception:
        pass
    return data

def read_latest_report_text(root_dir: str, limit_chars: int = 2000):
    """
    Читает последний analysis_*.md из reports/day или reports/mid.
    Используется как мягкий контекст (вариант A).
    """
    try:
        base = BASE / "reports" / root_dir
        roots = sorted(base.glob("*"))
        if not roots:
            return "", None
        d = roots[-1]
        an = sorted(d.glob("analysis_*.md"))
        if not an:
            return "", None

        report_time = None
        try:
            report_time = datetime.strptime(d.name, "%Y%m%d_%H%M%S")
        except ValueError:
            pass
        if report_time is None:
            report_time = datetime.fromtimestamp(d.stat().st_mtime)

        now = datetime.now()
        age_hours = max((now - report_time).total_seconds() / 3600, 0.0)

        txt = an[-1].read_text(encoding="utf-8").strip()
        if limit_chars and len(txt) > limit_chars:
            return txt[-limit_chars:], age_hours
        return txt, age_hours
    except Exception:
        return "", None

def main():
    p = BASE / "logs" / "last.json"
    if not p.exists():
        print("postprocess_full_last: logs/last.json not found")
        return

    data = json.loads(p.read_text(encoding="utf-8"))

    # 1) EMA20(M15/H1) для FULL, если отсутствуют
    try:
        sym = data.get("symbol")
        if sym:
            if not data.get("ema20_m15"):
                data["ema20_m15"] = get_ema20_m15(sym)
            if not data.get("ema20_h1"):
                data["ema20_h1"] = get_ema20_h1(sym)
    except Exception:
        pass

    # 2) Читаем DAY/MID тексты (вариант A)
    day_txt, day_age_hours = read_latest_report_text("day", limit_chars=2000)
    mid_txt, mid_age_hours = read_latest_report_text("mid", limit_chars=2000)

    day_context = day_txt
    if day_age_hours is not None and day_age_hours > 36:
        print(f"postprocess_full_last: DAY report too old ({day_age_hours:.1f}h), ignoring context")
        day_context = None

    mid_context = mid_txt
    if mid_age_hours is not None and mid_age_hours > 120:
        print(f"postprocess_full_last: MID report too old ({mid_age_hours:.1f}h), ignoring context")
        mid_context = None

    # 3) прогоняем общий v2-процессор
    data = pp_process(data, day_context, mid_context)
    data = _drop_time_window_mentions(data)
    try:
        apply_ema_exhale_filter(data)
    except Exception:
        pass

    # 4) сохраняем обратно
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("postprocess_full_last: OK")

if __name__ == "__main__":
    main()
