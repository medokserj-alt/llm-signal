from __future__ import annotations

import re


_LOCAL_INCIDENT_MARKERS = (
    "hospital",
    "patient",
    "nurse",
    "nurses",
    "member of the public",
    "police",
    "officer",
    "officers",
    "stabbing",
    "shooting",
    "gunman",
    "gunfire",
    "assault",
    "crime",
    "criminal",
    "murder",
    "kidnap",
    "robbery",
    "domestic violence",
)

_MACRO_MARKERS = (
    "central bank",
    "fed",
    "federal reserve",
    "fomc",
    "ecb",
    "boj",
    "boe",
    "pboc",
    "rates",
    "rate hike",
    "rate cut",
    "inflation",
    "cpi",
    "ppi",
    "pce",
    "gdp",
    "employment",
    "jobs",
    "jobless claims",
    "nonfarm",
    "nfp",
    "unemployment",
    "treasury yields",
    "bond yields",
)

_BANKING_MARKERS = (
    "banking stress",
    "banking crisis",
    "bank run",
    "deposit outflows",
    "liquidity facility",
    "emergency liquidity",
    "fdic",
    "major bank",
    "systemic bank",
    "credit stress",
    "funding stress",
)

_ENERGY_SHIPPING_MARKERS = (
    "hormuz",
    "strait of hormuz",
    "suez",
    "red sea",
    "shipping",
    "tanker",
    "oil",
    "gas",
    "lng",
    "pipeline",
    "blockade",
    "port closure",
)

_GEO_ACTORS = (
    "iran",
    "israel",
    "lebanon",
    "hezbollah",
    "hamas",
    "houthi",
    "houthis",
    "russia",
    "ukraine",
    "taiwan",
    "china",
    "u.s.",
    " us ",
    "us-",
    "white house",
    "oval office",
    "washington",
    "gaza",
)

_GEO_ESCALATION_MARKERS = (
    "war",
    "missile",
    "drone",
    "military",
    "strike",
    "sanctions",
    "terror",
    "terrorism",
    "terrorist",
    "blockade",
    "shipping",
    "retaliation",
    "troops",
    "ceasefire",
    "truce",
    "talks",
    "negotiation",
    "summit",
    "delegation",
)

_SYSTEMIC_GEO_MARKERS = (
    "border ceasefire",
    "fragile ceasefire",
    "truce may collapse",
    "truce at risk",
    "diplomats warn",
    "officials threaten",
    "military action",
    "renewed military",
    "escalation risk",
    "downside shock",
    "risk premium",
    "shipping insurers reprice",
)

_CRYPTO_ASSET_MARKERS = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "cryptocurrency",
    "digital asset",
    "digital assets",
    "stablecoin",
    "usdt",
    "usdc",
    "binance",
    "coinbase",
    "bybit",
    "kraken",
    "okx",
    "exchange",
    "etf",
    "sec",
    "custody",
    "custodian",
    "wallet",
    "blockchain",
    "token",
)

_CRYPTO_EVENT_MARKERS = (
    "etf",
    "sec",
    "regulation",
    "regulatory",
    "exchange",
    "custody",
    "custodian",
    "hack",
    "hacked",
    "exploit",
    "breach",
    "withdrawals",
    "outage",
    "liquidation",
    "lawsuit",
)

_DIRECT_CRYPTO_MARKET_STRUCTURE_MARKERS = (
    "exchange outage",
    "withdrawal halt",
    "withdrawals halted",
    "liquidation cascade",
    "major crypto exchange hack",
)

_DIRECT_MARKET_WIDE_GEO_MARKERS = (
    "interstate conflict",
    "major escalation",
    "naval blockade",
    "shipping disruption",
    "sanctions",
    "tariff",
    "terror attack",
    "terrorist attack",
)


def _blob(*parts: str) -> str:
    text = " ".join(part for part in parts if isinstance(part, str))
    low = text.lower()
    low = re.sub(r"\s+", " ", low).strip()
    return low


def _contains_any(blob: str, needles: tuple[str, ...]) -> bool:
    for needle in needles:
        phrase = str(needle or "").strip().lower()
        if not phrase:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
        if re.search(pattern, blob):
            return True
    return False


def is_local_public_safety_incident(title: str, summary: str = "", link: str = "") -> bool:
    blob = _blob(title, summary, link)
    if not _contains_any(blob, _LOCAL_INCIDENT_MARKERS):
        return False
    if _contains_any(blob, _MACRO_MARKERS + _BANKING_MARKERS + _ENERGY_SHIPPING_MARKERS):
        return False
    if _contains_any(blob, _CRYPTO_ASSET_MARKERS + _CRYPTO_EVENT_MARKERS):
        return False
    if _contains_any(blob, _DIRECT_MARKET_WIDE_GEO_MARKERS):
        return False
    return not (_contains_any(blob, _GEO_ACTORS) and _contains_any(blob, _GEO_ESCALATION_MARKERS))


def classify_market_relevance(title: str, summary: str = "", link: str = "") -> dict[str, object]:
    blob = _blob(title, summary, link)
    local_incident = is_local_public_safety_incident(title, summary=summary, link=link)
    macro = _contains_any(blob, _MACRO_MARKERS)
    banking = _contains_any(blob, _BANKING_MARKERS)
    energy_shipping = _contains_any(blob, _ENERGY_SHIPPING_MARKERS)
    geopolitics = _contains_any(blob, _DIRECT_MARKET_WIDE_GEO_MARKERS + _SYSTEMIC_GEO_MARKERS) or (
        _contains_any(blob, _GEO_ACTORS) and _contains_any(blob, _GEO_ESCALATION_MARKERS)
    )
    crypto = _contains_any(blob, _DIRECT_CRYPTO_MARKET_STRUCTURE_MARKERS) or (
        _contains_any(blob, _CRYPTO_EVENT_MARKERS) and _contains_any(blob, _CRYPTO_ASSET_MARKERS)
    )

    category = ""
    if local_incident:
        category = "local_incident"
    elif macro:
        category = "macro"
    elif banking:
        category = "banking"
    elif energy_shipping:
        category = "energy_shipping"
    elif geopolitics:
        category = "geopolitics"
    elif crypto:
        category = "crypto"

    return {
        "market_relevant": bool(category and category != "local_incident"),
        "category": category,
        "local_incident": local_incident,
    }


def is_market_relevant_news_item(title: str, summary: str = "", link: str = "") -> bool:
    return bool(classify_market_relevance(title, summary=summary, link=link).get("market_relevant"))
