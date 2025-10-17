import sys, json
from schema import SignalPayload

def fail(msgs):
    print("VALIDATION FAILED:")
    for m in msgs:
        print(f"- {m}")
    sys.exit(2)

def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"Invalid JSON: {e}")
        sys.exit(1)

    try:
        s = SignalPayload.model_validate(data)
    except Exception as e:
        print(f"Pydantic validation error: {e}")
        sys.exit(1)

    errs = []

    # базовые проверки
    if not (s.entry_range.min < s.entry_range.max):
        errs.append(f"entry_range.min ({s.entry_range.min}) должен быть < entry_range.max ({s.entry_range.max}).")

    if s.rr <= 0:
        errs.append(f"rr ({s.rr}) должен быть > 0.")

    if s.validity_minutes <= 0:
        errs.append(f"validity_minutes ({s.validity_minutes}) должен быть > 0.")

    # направленные проверки
    if s.direction == "long":
        if not (s.sl < s.entry_range.min):
            errs.append(f"LONG: SL ({s.sl}) должен быть < нижней границы входа ({s.entry_range.min}).")
        if not (s.tp1 > s.entry_range.max):
            errs.append(f"LONG: TP1 ({s.tp1}) должен быть > верхней границы входа ({s.entry_range.max}).")
        if not (s.tp2 > s.entry_range.max):
            errs.append(f"LONG: TP2 ({s.tp2}) должен быть > верхней границы входа ({s.entry_range.max}).")
        if s.tp2 < s.tp1:
            errs.append(f"LONG: TP2 ({s.tp2}) обычно ≥ TP1 ({s.tp1}).")
    elif s.direction == "short":
        if not (s.sl > s.entry_range.max):
            errs.append(f"SHORT: SL ({s.sl}) должен быть > верхней границы входа ({s.entry_range.max}).")
        if not (s.tp1 < s.entry_range.min):
            errs.append(f"SHORT: TP1 ({s.tp1}) должен быть < нижней границы входа ({s.entry_range.min}).")
        if not (s.tp2 < s.entry_range.min):
            errs.append(f"SHORT: TP2 ({s.tp2}) должен быть < нижней границы входа ({s.entry_range.min}).")
        if s.tp2 > s.tp1:
            errs.append(f"SHORT: TP2 ({s.tp2}) обычно ≤ TP1 ({s.tp1}).")

    if errs:
        fail(errs)

    print("OK: logical checks passed.")
    sys.exit(0)

if __name__ == "__main__":
    main()
