#!/usr/bin/env python3
import sys, time, feedparser, re
from datetime import datetime, timezone, timedelta

FEEDS = [
  "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
  "https://cointelegraph.com/rss",
]
SYMS = {"BTC","ETH","SOL","AVAX","SUI","APT","AAVE","UNI","DOGE","PEPE","GALA","LINK"}
CUT_HOURS = int(sys.argv[1]) if len(sys.argv)>1 else 12

now = datetime.now(timezone.utc)
cut = now - timedelta(hours=CUT_HOURS)

out = []
for url in FEEDS:
    d = feedparser.parse(url)
    for e in d.entries:
        # published_parsed может отсутствовать
        t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if not t: continue
        dt = datetime(*t[:6], tzinfo=timezone.utc)
        if dt < cut: continue
        title = e.title.strip()
        # фильтр по символам пула (хватает простого текста)
        if not any(s in title.upper() for s in SYMS):
            # пропускаем нерелевантное (оставим немного общих макро новостей)
            if not re.search(r'ETF|SEC|FED|inflation|macro|alts|altcoin', title, re.I):
                continue
        out.append(f"- {title} ({dt.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')})")

# небольшое ограничение, чтобы не перегружать модель
print("\n".join(out[:10]) if out else "— за последние часы значимых заголовков по пулу не найдено")
