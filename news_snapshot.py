#!/usr/bin/env python3
import sys
import time
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import feedparser
from market_relevance import is_market_relevant_news_item

# ---------------------------
#  Конфигурация RSS-источников
# ---------------------------

# КРИПТО / БЛОКЧЕЙН (разные издатели)
CRYPTO_FEEDS = [
    # Основные
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    # Дополнительные
    "https://news.bitcoin.com/feed/",
    "https://blockworks.co/feed",
    "https://www.coindesk.com/markets/feed/",
]

# МАКРО / РЫНКИ / ПОЛИТИКА
MACRO_FEEDS = [
    # Reuters — общие/бизнес/рынки
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.reuters.com/reuters/USmarkets",
    # MarketWatch — топ-статьи и рынки
    "https://www.marketwatch.com/feeds/topstories",
    "https://www.marketwatch.com/feeds/marketpulse",
    # Financial Times (часть материалов может быть платной, но заголовки есть)
    "https://www.ft.com/?format=rss",
    # General world / geopolitics fallback
    "https://www.theguardian.com/world/rss",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

# Ключевые слова для выделения макро-тем
MACRO_KEYWORDS = [
    "shutdown", "cpi", "inflation", "fed", "federal reserve", "fomc",
    "rates", "rate hike", "rate cut", "treasury", "yields",
    "jobs", "nonfarm", "nfp", "unemployment",
    "stimulus", "congress", "debt ceiling", "government shutdown",
]

GEOPOLITICS_KEYWORDS = [
    "white house", "oval office", "press conference", "briefing", "remarks",
    "talks", "meeting", "summit", "diplomatic", "delegation",
    "ceasefire", "truce", "deadline", "extension possible",
    "israel", "lebanon", "hezbollah", "iran", "hormuz",
    "blockade", "sanctions", "tariff", "shipping",
]

# Максимум выводимых строк
MAX_ITEMS = 60


# ---------------------------
#  Хелперы
# ---------------------------

def _guess_impact(title: str) -> str:
    t = (title or "").lower()
    positive = ["surge", "soar", "jumps", "rally", "spike", "up", "gain", "recovers", "bull"]
    negative = ["plunge", "falls", "fall", "tumble", "down", "drop", "dump",
                "bleed", "bear", "weakness", "shutdown"]
    if any(w in t for w in positive) and not any(w in t for w in negative):
        return "+"
    if any(w in t for w in negative) and not any(w in t for w in positive):
        return "−"
    return "neutral"


def _is_macro_item(title: str, summary: str, link: str, from_macro_feed: bool) -> bool:
    """
    Грубый классификатор: если новость из MACRO_FEEDS — считаем макро.
    Иначе смотрим, есть ли ключевые слова в заголовке/описании/ссылке.
    """
    blob = " ".join([title or "", summary or "", link or ""]).lower()
    keyword_match = any(k in blob for k in MACRO_KEYWORDS) or any(k in blob for k in GEOPOLITICS_KEYWORDS)
    if keyword_match:
        return True
    if from_macro_feed:
        return is_market_relevant_news_item(title, summary=summary, link=link)
    return False


def fetch_items(hours: int) -> list[dict]:
    """
    Сбор новостей из крипто- и макро-RSS за последние `hours` часов.
    Возвращает список словарей:
      { when: ts, line: готовая строка, is_macro: bool }
    """
    now = time.time()
    items: list[dict] = []

    def process_feed(url: str, tag_macro: bool):
        try:
            feed = feedparser.parse(url)
        except Exception:
            return
        for e in getattr(feed, "entries", []):
            title = getattr(e, "title", "").strip()
            if not title:
                continue
            link = getattr(e, "link", "").strip()
            summary = getattr(e, "summary", "") or ""

            published_parsed = getattr(e, "published_parsed", None) \
                               or getattr(e, "updated_parsed", None)
            ts_value = None
            if published_parsed:
                ts = time.mktime(published_parsed)
                age_hours = (now - ts) / 3600.0
                ts_value = ts
            else:
                # нет даты — считаем очень свежей
                age_hours = 0.0

            if age_hours > hours:
                continue

            is_macro = _is_macro_item(title, summary, link, tag_macro)
            impact = _guess_impact(title)

            ts_prefix = ""
            if ts_value is not None:
                try:
                    dt_msk = datetime.fromtimestamp(
                        ts_value, tz=timezone.utc
                    ).astimezone(ZoneInfo("Europe/Moscow"))
                    ts_str = dt_msk.strftime("%Y-%m-%d %H:%M")
                    ts_prefix = f"[{ts_str} МСК] "
                except Exception:
                    ts_prefix = ""

            body = f"[impact:{impact}] {title}"
            if link:
                body = f"{body} — {link}"
            line = f"- {ts_prefix}{body}".rstrip()
            items.append({
                "when": ts_value if ts_value is not None else now - age_hours * 3600.0,
                "line": line,
                "is_macro": is_macro,
            })

    # 1) крипто-лента
    for u in CRYPTO_FEEDS:
        process_feed(u, tag_macro=False)
    # 2) макро-лента
    for u in MACRO_FEEDS:
        process_feed(u, tag_macro=True)

    # сортировка:
    #   сначала макро, потом крипта; внутри — от новых к старым
    items.sort(key=lambda x: (0 if x["is_macro"] else 1, -x["when"]))
    return items[:MAX_ITEMS]


def main():
    try:
        hours = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    except Exception:
        hours = 12

    items = fetch_items(hours)
    if not items:
        print("- [impact:neutral] NEWS feed is empty or unavailable")
        sys.exit(0)

    for it in items:
        print(it["line"])


if __name__ == "__main__":
    main()
