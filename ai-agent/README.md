# AI Agent backend (FastAPI + LangGraph)

Этот сервис — отдельный backend-агент для работы вокруг Telegram-бота сигналов:

- принимает структурированные сигналы,
- ведёт журнал событий,
- оценивает сигналы (подготовка к TP/SL-логике),
- формирует отчёты по сделкам,
- даёт дневную сводку (daily summary).

Пока живёт как отдельный сервис, без жёсткой привязки к боту.


## Стек

- Python 3.12
- FastAPI
- Uvicorn
- LangGraph / LangChain
- pydantic / pydantic-settings


## Структура проекта

\`\`\`text
ai-agent/
  app/
    api/
      telegram.py   # входные точки /tg/*
      jobs.py       # джобы /jobs/*
      reports.py    # отчёты /jobs/*
    agent/
      graph.py      # граф агента (LangGraph, пока echo + журнал)
      tools.py      # journal_event и вспом. инcтрументы
      evaluator.py  # логика оценки сигналов → trade_result
      market_data.py# заглушка источника цен (потом Bybit/CMC/...)
      reports.py    # сборка отчётов по сделкам и дню
    core/
      config.py     # настройки (BaseSettings)
      logging.py    # базовый logging
  logs/
    journal.jsonl   # журнал событий (signal, feedback, trade_result)
  .venv/            # venv (не в гите)
  requirements.txt
  README.md
\`\`\`


## Запуск

Из каталога \`ai-agent\`:

\`\`\bash
python3 -m venv .venv
source .venv/bin/activate
pip install --break-system-packages -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
\`\`\`

Проверка:

\`\`\bash
curl http://localhost:8001/health
# {"status":"ok"}
\`\`\`


## Формат сигнала (Signal JSON v1)

Сигнал присылается dev-ботом в \`POST /tg/signal\`:

\`\`\json
{
  "signal_id": "SUI-2025-11-16-02",
  "symbol": "SUIUSDT",
  "direction": "short",
  "entry_zone": [0.088, 0.090],
  "sl": 0.093,
  "tp": {
    "tp1": 0.083,
    "tp2": 0.079
  },
  "published_at": "2025-11-16T09:10:00Z",
  "channel_id": "@dev_channel",
  "meta": {
    "r_r": 3.2,
    "strategy": "strict"
  }
}
\`\`\`

Минимальная проверка структуры (sanity-check) уже реализована в \`/tg/signal\`.


## Журнал событий (journal.jsonl)

В \`logs/journal.jsonl\` пишутся JSON-строки с полями:

- \`ts\` — unix timestamp записи,
- \`kind\` — тип события:
  - \`raw_update\` — сырые апдейты (через /tg/webhook),
  - \`feedback\` — ручной/пользовательский фидбек,
  - \`signal\` — сохранённый сигнал,
  - \`trade_result\` — результат оценки сигнала,
- \`event\` — полезная нагрузка (структура зависит от kind).


## Эндпоинты

### 1. \`GET /health\`

Проверка живости сервиса.

---

### 2. \`POST /tg/webhook\`

Пока используется как тестовый вход для эхо-агента.  
\`run_agent\` в \`graph.py\` просто возвращает echo + записывает raw_update в журнал.

---

### 3. \`POST /tg/feedback\`

Принимает произвольный JSON-фидбек и пишет в журнал как:

\`\`\json
{"kind": "feedback", "event": {...}}
\`\`\`

---

### 4. \`POST /tg/signal\`

Принимает Signal JSON v1 (см. выше) и пишет в журнал:

\`\`\json
{"kind": "signal", "event": {...}}
\`\`\`

Используется как основная точка для регистрации сигналов от dev-бота.

---

### 5. \`POST /jobs/eval_signals\`

Запускает оценку сигналов:

- ищет \`kind="signal"\`, у которых \`published_at\` старше \`window_minutes\`,
- для каждого сигнала:
  - вытаскивает \`symbol\`, \`entry_zone\`, \`sl\`, \`tp\`,
  - запрашивает ценовую серию через \`fetch_price_series\`,
  - вычисляет исход: \`TP1\`, \`TP2\`, \`SL\`, \`NO_TRIGGER\`, \`TIMEOUT\`, \`NO_DATA\`,
  - пишет \`kind="trade_result"\` в журнал.

Сейчас \`fetch_price_series\` — заглушка, поэтому исход \`NO_DATA\`.  
Подключение реальных цен делается внутри \`market_data.py\`.

---

### 6. \`GET /jobs/trade_report?signal_id=...\`

Возвращает структурный и текстовый отчёт по сделке:

- находит \`signal\` и \`trade_result\` по \`signal_id\`,
- собирает JSON:
  - \`signal\`,
  - \`result\`,
  - \`summary\` — текст для отправки в канал.

Пример использования:

\`\`\bash
curl "http://localhost:8001/jobs/trade_report?signal_id=SUI-2025-11-16-02"
\`\`\`

---

### 7. \`GET /jobs/daily_summary?window_hours=24\`

Агрегированная сводка по всем \`trade_result\` за период:

- \`total_trades\` — сколько сделок оценено,
- \`by_outcome\` — сколько TP/SL/NO_DATA и т.п.,
- \`by_symbol\` — распределение по инструментам,
- \`summary\` — текстовая сводка.

Пример:

\`\`\bash
curl "http://localhost:8001/jobs/daily_summary?window_hours=24"
\`\`\`


## Как dev-бот будет использовать агента

(Когда до этого дойдём.)

1. При публикации сигнала:
   - отправить сообщение в Telegram-канал,
   - отправить Signal JSON v1 в \`POST /tg/signal\`.

2. Периодически (cron/джоба):
   - вызывать \`POST /jobs/eval_signals\`.

3. Для отчёта по сделке:
   - вызывать \`GET /jobs/trade_report?signal_id=...\`,
   - брать \`summary\` и постить в канал.

4. Для дневного отчёта:
   - вызывать \`GET /jobs/daily_summary\`,
   - постить \`summary\` в канал / внутренний чат.

