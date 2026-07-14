from __future__ import annotations

import copy
import re

from event_calendar import utc_now_iso
from market_relevance import classify_market_relevance

_VALID_CATEGORIES = {
    "geopolitics",
    "macro_policy_shock",
    "crypto_market_structure",
}
_VALID_PHASES = {"pre_event", "ongoing", "post_event"}
_VALID_IMPACTS = {"high", "medium", "low"}
_VALID_DIRECTIONAL_RISKS = {"risk_on", "risk_off", "uncertain", "mixed", "neutral"}
_VALID_CRITICAL_SEVERITIES = {"medium", "high", "severe"}
_VALID_CONFIRM_POLICIES = {"normal", "defensive", "block_stale_confirm"}

_STOPWORDS = {
    "a",
    "an",
    "and",
    "ahead",
    "after",
    "agreement",
    "announces",
    "announced",
    "are",
    "as",
    "at",
    "before",
    "begin",
    "begins",
    "collapse",
    "continues",
    "deal",
    "expected",
    "fails",
    "for",
    "from",
    "hold",
    "holds",
    "in",
    "into",
    "is",
    "meeting",
    "meetings",
    "negotiation",
    "negotiations",
    "of",
    "on",
    "ongoing",
    "planned",
    "post",
    "pre",
    "resume",
    "resumes",
    "set",
    "talk",
    "talks",
    "the",
    "to",
    "underway",
    "without",
}

_PRE_EVENT_MARKERS = (
    "ahead of",
    "before",
    "expected",
    "planned",
    "set to",
    "scheduled to",
    "upcoming",
    "later this",
    "next week",
    "next round",
    "tomorrow",
    "this weekend",
    "to resume",
    "to meet",
    "to speak",
    "remarks later",
    "press conference later",
    "briefing later",
    "deadline nears",
    "deadline near",
    "due to expire",
    "set to expire",
)
_ONGOING_MARKERS = (
    "ongoing",
    "under way",
    "underway",
    "in talks",
    "negotiations continue",
    "talks continue",
    "talks begin",
    "begins talks",
    "begin talks",
    "meeting is underway",
    "meeting under way",
    "resumes talks",
    "resume talks",
)
_POST_EVENT_MARKERS = (
    "without agreement",
    "no agreement",
    "failed talks",
    "talks failed",
    "ended without",
    "ends without",
    "deal reached",
    "agreement reached",
    "ceasefire reached",
    "announced",
    "imposed",
    "launched",
    "struck",
    "attack",
    "attacks",
    "exploit",
    "hacked",
    "hack",
    "outage",
    "halted withdrawals",
    "liquidation cascade",
    "liquidations",
    "crashes",
    "emergency",
)

_SCHEDULED_EVENT_NEEDLES = (
    "cpi",
    "ppi",
    "pce",
    "nfp",
    "jobless claims",
    "powell",
    "fomc",
    "fed minutes",
    "minutes",
    "fed speaker",
    "federal reserve",
    "nonfarm payrolls",
)
_UNSCHEDULED_OVERRIDE_NEEDLES = (
    "emergency",
    "unexpected",
    "surprise",
    "shock",
    "tariff",
    "sanctions",
    "blockade",
    "strait of hormuz",
    "hormuz",
)

_GEOPOLITICS_NEEDLES = (
    "talks",
    "negotiation",
    "ceasefire",
    "truce",
    "diplomatic",
    "diplomacy",
    "summit",
    "meeting",
    "delegation",
    "sanctions",
    "tariff",
    "blockade",
    "shipping",
    "hormuz",
    "strait of hormuz",
    "red sea",
    "strike",
    "missile",
    "attack",
    "military",
    "retaliation",
)
_MACRO_POLICY_NEEDLES = (
    "emergency",
    "central bank",
    "fed emergency",
    "surprise tariff",
    "unexpected tariff",
    "policy shock",
    "sanctions shock",
    "capital controls",
    "emergency liquidity",
    "emergency cut",
    "emergency hike",
    "unexpected inflation",
    "unexpected growth",
)
_CRYPTO_MARKET_STRUCTURE_NEEDLES = (
    "liquidation",
    "liquidations",
    "liquidation cascade",
    "exchange",
    "withdrawals",
    "halt",
    "outage",
    "exploit",
    "hack",
    "breach",
    "etf",
    "sec",
    "lawsuit",
    "regulatory",
    "institutional",
)
_CRYPTO_SHOCK_NEEDLES = (
    "liquidation",
    "exchange",
    "withdrawals",
    "outage",
    "exploit",
    "hack",
    "breach",
    "sec",
    "lawsuit",
    "etf",
    "regulatory",
)

_RISK_OFF_NEEDLES = (
    "without agreement",
    "no agreement",
    "failed",
    "breakdown",
    "escalation",
    "strike",
    "attack",
    "sanctions",
    "tariff",
    "blockade",
    "shipping threat",
    "hormuz",
    "liquidation",
    "outage",
    "hack",
    "exploit",
    "lawsuit",
    "denied",
    "emergency",
)
_RISK_ON_NEEDLES = (
    "agreement reached",
    "deal reached",
    "ceasefire reached",
    "ceasefire extended",
    "truce extended",
    "ceasefire extension",
    "truce extension",
    "extended by three weeks",
    "extended by 3 weeks",
    "de-escalation",
    "sanctions relief",
    "reopened",
    "resume shipping",
    "approved",
    "approval",
    "backstop",
    "support package",
)

_STABLECOIN_ADOPTION_NEEDLES = (
    "stablecoin",
    "stablecoins",
    "usdt",
    "usdc",
)
_STABLECOIN_ADOPTION_CONTEXT_NEEDLES = (
    "adoption",
    "adopt",
    "dominates",
    "dominate",
    "dominant",
    "purchases",
    "purchase",
    "payments",
    "payment",
    "settlement",
    "settlements",
    "transactions",
    "transaction",
    "usage",
    "used",
    "volume",
)
_STABLECOIN_POLICY_SHOCK_NEEDLES = (
    "ban",
    "bans",
    "banned",
    "prohibit",
    "prohibits",
    "prohibited",
    "crackdown",
    "emergency",
    "depeg",
    "bank run",
    "banking stress",
    "systemic risk",
    "systemic bank",
    "lawsuit",
    "sanctions",
)


def _is_stablecoin_adoption_story(text: str) -> bool:
    blob = _text(text).lower()
    def _contains_phrase(needles) -> bool:
        for needle in needles:
            phrase = _text(needle).lower()
            if not phrase:
                continue
            pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
            if re.search(pattern, blob):
                return True
        return False

    return _contains_phrase(_STABLECOIN_ADOPTION_NEEDLES) and _contains_phrase(
        _STABLECOIN_ADOPTION_CONTEXT_NEEDLES
    ) and not _contains_phrase(_STABLECOIN_POLICY_SHOCK_NEEDLES)

_TALKS_FAILED_MARKERS = (
    "without agreement",
    "no agreement",
    "failed talks",
    "talks failed",
    "ended without",
    "ends without",
    "talks collapse",
    "talks collapsed",
    "peace talks fail",
    "peace talks failed",
    "negotiations failed",
    "response was rejected",
    "rejected the response",
    "rejected iran's response",
    "rejecting iran's response",
    "rejected iran’s response",
    "rejecting iran’s response",
    "peace proposal rejected",
    "rejected peace proposal",
)
_TALKS_ONGOING_MARKERS = (
    "ongoing",
    "under way",
    "underway",
    "in talks",
    "negotiations continue",
    "talks continue",
    "talks begin",
    "begins talks",
    "begin talks",
    "meeting is underway",
    "meeting under way",
    "resumes talks",
    "resume talks",
    "talks begin at white house",
)
_TALKS_UPCOMING_MARKERS = _PRE_EVENT_MARKERS
_MIXED_TRANSITION_MARKERS = (
    " as ",
    " while ",
    " but ",
    " after ",
    " amid ",
    " with ",
)
_CONFIRMED_GEO_ESCALATION_MARKERS = (
    "sanctions announced",
    "sanctions imposed",
    "tariffs announced",
    "tariffs imposed",
    "blockade imposed",
    "blockade in force",
    "shipping halted",
    "shipping disrupted",
    "strait closed",
    "strike confirmed",
    "strike launched",
    "missile strike",
    "attack confirmed",
    "attacks launched",
    "launches attacks",
    "launched attacks",
    "attacks on american military facilities",
    "fresh us strikes",
    "fresh u.s. strikes",
    "military base attacked",
    "military bases attacked",
    "retaliation launched",
)
_ESCALATION_RISK_MARKERS = (
    "risk increased",
    "risk rises",
    "risk rose",
    "risk remains",
    "risk stays",
    "in focus",
    "under threat",
    "may happen",
    "may collapse",
    "could collapse",
    "could follow",
    "looms",
    "possible",
)

_GEO_CEASEFIRE_RISK_MARKERS = (
    "ceasefire may collapse",
    "ceasefire could collapse",
    "ceasefire at risk",
    "fragile ceasefire",
    "ceasefire on life support",
    "ceasefire is on life support",
    "ceasefire on massive life support",
    "ceasefire is on massive life support",
    "massive life support",
    "life support",
    "ceasefire frays",
    "ceasefire fraying",
    "truce may collapse",
    "truce at risk",
    "ceasefire collapse risk",
    "truce collapse risk",
    "ceasefire violation",
    "truce violation",
)
_GEO_DIPLOMATIC_BREAKDOWN_MARKERS = (
    "talks failed",
    "talks ended",
    "without agreement",
    "no agreement",
    "negotiations failed",
    "negotiations ended",
    "diplomatic breakdown",
    "diplomatic path collapsed",
    "diplomatic path failed",
    "rejected the response",
    "response was rejected",
    "rejected iran's response",
    "rejecting iran's response",
    "rejected iran’s response",
    "rejecting iran’s response",
    "rejected peace proposal",
    "peace proposal rejected",
)
_GEO_MILITARY_THREAT_MARKERS = (
    "military action",
    "military response",
    "military strike",
    "military threat",
    "attack risk",
    "strike risk",
    "retaliation risk",
    "retaliation was confirmed",
    "retaliation was launched",
    "attack confirmed",
    "strike confirmed",
    "strike launched",
    "missile strike",
    "attack launched",
    "renewed military",
)
_HEADLINE_MILITARY_ESCALATION_MARKERS = (
    "will strike iran",
    "strike iran very strongly",
    "strong strikes on iran",
    "strikes today",
    "strikes tomorrow",
    "natanz",
    "nuclear site threat",
    "missile and drone attacks",
    "attacks across gulf",
    "houthis fire missiles",
    "сша намерены нанести",
    "сильные удары по ирану",
    "сильный удар по ирану",
    "сегодня мы нанесем",
    "завтра тоже нанесем",
    "натанз",
    "ядерному объекту",
    "бомбардировке",
    "military action",
    "military response",
    "military strike",
    "military escalation",
    "strike launched",
    "strikes launched",
    "launches strikes",
    "launched strikes",
    "airstrike",
    "airstrikes",
    "missile strike",
    "attack confirmed",
    "attack launched",
    "retaliation launched",
    "struck iran",
    "struck iranian",
)
_HEADLINE_SHIPPING_ENERGY_ESCALATION_MARKERS = (
    "naval blockade",
    "blockade of iranian shipping",
    "entire iranian coastline",
    "all vessels",
    "control of the strait of hormuz",
    "oil up 9%",
    "hormuz closure",
    "strait of hormuz closure",
    "threatens hormuz closure",
    "threatens to block",
    "threat to block",
    "blockade imposed",
    "shipping halted",
    "shipping disrupted",
    "strait closed",
    "oil supply shock",
    "energy shock",
    "tanker attacks",
    "oil tankers come under fire",
)
_HEADLINE_DEESCALATION_MARKERS = (
    "ceasefire agreement confirmed",
    "ceasefire reached",
    "ceasefire extended",
    "truce reached",
    "truce extended",
    "de-escalation confirmed",
    "deescalation confirmed",
    "shipping resumed",
    "strait reopened",
    "sanctions relief",
    "abandon proposed",
    "lift blockade",
    "lifts blockade",
    "reopen shipping",
    "reopens shipping",
    "talks resume",
    "talks resumed",
    "tensions ease",
)
_GEO_NEGOTIATION_MARKERS = (
    "talks",
    "negotiation",
    "negotiations",
    "ceasefire",
    "truce",
    "diplomatic",
    "summit",
    "meeting",
    "delegation",
)
_GEO_ESCALATION_SIGNAL_MARKERS = (
    "warned",
    "warning",
    "threat",
    "threatened",
    "threatens",
    "vowed",
    "vows",
    "pledged",
    "signaled",
    "signals",
    "ultimatum",
    "deadline",
    "all options",
    "response remains on the table",
    "public statement",
)
_GEO_DOWNSIDE_SHOCK_MARKERS = (
    "shipping",
    "energy",
    "oil",
    "blockade",
    "sanctions",
    "tariff",
    "attack",
    "strike",
    "military",
    "retaliation",
    "disruption",
    "downside shock",
    "tail risk",
    "risk premium",
)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _normalize_category(value) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "macro": "macro_policy_shock",
        "policy": "macro_policy_shock",
        "crypto": "crypto_market_structure",
        "market_structure": "crypto_market_structure",
        "politics": "geopolitics",
    }
    text = aliases.get(text, text)
    return text if text in _VALID_CATEGORIES else ""


def _normalize_phase(value) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    return text if text in _VALID_PHASES else ""


def _normalize_impact(value) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    if text in {"critical", "very_high", "veryhigh", "severe"}:
        return "high"
    if text in {"moderate", "med"}:
        return "medium"
    if text in {"minor"}:
        return "low"
    return text if text in _VALID_IMPACTS else ""


def _normalize_directional_risk(value) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    return text if text in _VALID_DIRECTIONAL_RISKS else ""


def _split_fragments(value) -> list[str]:
    text = _text(value)
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for piece in re.split(r"(?<=[.!?])\s+|\s*[;•]+\s*", text):
        candidate = piece.strip()
        if not candidate:
            continue
        key = _topic_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _merge_unique_texts(*values, max_items: int | None = None) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for fragment in _split_fragments(value):
            key = _topic_key(fragment)
            if key in seen:
                continue
            seen.add(key)
            out.append(fragment)
            if max_items is not None and len(out) >= max_items:
                return " ".join(out)
    return " ".join(out)


def _merge_unique_drivers(*values, max_items: int = 4) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = _text(item)
            if not text:
                continue
            key = _topic_key(text)
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= max_items:
                return out
    return out


def _normalize_text_list(values, *, max_items: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    items = values if isinstance(values, list) else [values]
    for item in items:
        text = _text(item)
        if not text:
            continue
        key = _topic_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if max_items is not None and len(out) >= max_items:
            break
    return out


def _topic_key(value) -> str:
    text = _text(value).lower()
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^0-9a-zа-яё\s]", " ", text)
    tokens = []
    for token in re.findall(r"[0-9a-zа-яё]+", text):
        if token in _STOPWORDS:
            continue
        tokens.append(token)
    return " ".join(tokens).strip()


def _topic_identity(value) -> str:
    text = _text(value).lower()
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text)
    for needle in (
        "without agreement",
        "no agreement",
        "failed",
        "ended",
        "expected",
        "ongoing",
        "underway",
        "under way",
        "announced",
        "launched",
        "approved",
    ):
        text = text.replace(needle, " ")
    return _topic_key(text)


def _catalyst_phrase_key(value) -> str:
    text = _text(value).lower()
    if not text:
        return ""
    replacements = (
        ("ended without agreement", "failed"),
        ("ends without agreement", "failed"),
        ("ended without", "failed"),
        ("ends without", "failed"),
        ("talks collapse", "talks failed"),
        ("talks collapsed", "talks failed"),
        ("peace talks fail", "talks failed"),
        ("peace talks failed", "talks failed"),
        ("risk increased", "risk"),
        ("risk remains elevated", "risk"),
        ("risk remains", "risk"),
        ("risk stays", "risk"),
        ("are still ahead", "ahead"),
        ("was imposed", "imposed"),
        ("was confirmed", "confirmed"),
    )
    for needle, replacement in replacements:
        text = text.replace(needle, replacement)
    return _topic_key(text)


def _text_specificity(value: str) -> int:
    low = _text(value).lower()
    if not low:
        return 0
    score = len(low)
    if any(marker in low for marker in ("hormuz", "strait of hormuz", "us-iran", "shipping", "blockade", "muscat")):
        score += 20
    if any(marker in low for marker in ("confirmed", "imposed", "failed", "ongoing", "resumed")):
        score += 10
    return score


def _merge_semantic_text_list(values, *, max_items: int | None = None) -> list[str]:
    out: list[str] = []
    key_to_idx: dict[str, int] = {}
    items = values if isinstance(values, list) else [values]
    for item in items:
        text = _text(item)
        if not text:
            continue
        key = _catalyst_phrase_key(text)
        if not key:
            continue
        idx = key_to_idx.get(key)
        if idx is None:
            key_to_idx[key] = len(out)
            out.append(text)
            continue
        if _text_specificity(text) > _text_specificity(out[idx]):
            out[idx] = text
    if max_items is not None:
        return out[:max_items]
    return out


def _unique_text_tokens(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values if isinstance(values, list) else [values]:
        text = _text(item)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _impact_rank(value: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(_text(value).lower(), 0)


def _regime_severity_rank(value: str) -> int:
    return {"": 0, "low": 1, "medium": 2, "high": 3, "severe": 4}.get(_text(value).lower(), 0)


def _stronger_regime_severity(*values) -> str:
    best = ""
    best_rank = -1
    for value in values:
        rank = _regime_severity_rank(_text(value))
        if rank > best_rank:
            best = _text(value).lower()
            best_rank = rank
    return best if best in {"low", "medium", "high", "severe"} else ""


def _stronger_impact(*values: str) -> str:
    strongest = ""
    for value in values:
        if _impact_rank(value) > _impact_rank(strongest):
            strongest = value
    return strongest


def _geopolitics_actor_tokens(value) -> tuple[str, ...]:
    low = _text(value).lower()
    actors: list[str] = []
    if any(marker in low for marker in ("u.s.", " us ", "us-", "trump", "washington", "american", "america")):
        actors.append("us")
    if any(marker in low for marker in ("iran", "tehran")):
        actors.append("iran")
    if "israel" in low:
        actors.append("israel")
    if "lebanon" in low:
        actors.append("lebanon")
    if "hezbollah" in low:
        actors.append("hezbollah")
    if "hamas" in low:
        actors.append("hamas")
    if "houthi" in low or "houthis" in low:
        actors.append("houthis")
    return tuple(sorted(set(actors)))


def _geopolitics_location_tokens(value) -> tuple[str, ...]:
    low = _text(value).lower()
    out: list[str] = []
    locations = (
        ("strait of hormuz", "hormuz"),
        ("hormuz", "hormuz"),
        ("muscat", "muscat"),
        ("oman", "oman"),
        ("bahrain", "bahrain"),
        ("kuwait", "kuwait"),
        ("red sea", "red_sea"),
        ("gaza", "gaza"),
        ("lebanon", "lebanon"),
        ("israel", "israel"),
        ("white house", "washington"),
        ("oval office", "washington"),
    )
    for needle, label in locations:
        if needle in low and label not in out:
            out.append(label)
    return tuple(out)


def _geopolitics_theme_tokens(value) -> tuple[str, ...]:
    low = _text(value).lower()
    themes: list[str] = []
    if _has_talks_context(low):
        themes.append("talks")
    if "blockade" in low:
        themes.append("blockade")
    if "shipping" in low:
        themes.append("shipping")
    if any(marker in low for marker in ("strike", "missile", "attack", "military")):
        themes.append("military")
    if any(marker in low for marker in ("escalation", "retaliation")):
        themes.append("escalation")
    if "ceasefire" in low:
        themes.append("ceasefire")
    if "sanctions" in low:
        themes.append("sanctions")
    if "tariff" in low:
        themes.append("tariffs")
    return tuple(sorted(set(themes)))


def _geopolitics_region_key(locations: tuple[str, ...]) -> str:
    if any(location in {"hormuz", "muscat", "oman"} for location in locations):
        return "gulf"
    if "red_sea" in locations:
        return "red_sea"
    if any(location in {"lebanon", "israel", "gaza"} for location in locations):
        return "levant"
    if locations:
        return locations[0]
    return ""


def _event_label_from_interpretation(interpretation: dict | None = None) -> str:
    interpretation = interpretation or {}
    confirmed_facts = interpretation.get("confirmed_facts") or []
    anticipated_consequences = interpretation.get("anticipated_consequences") or []
    realized_market_events = interpretation.get("realized_market_events") or []
    recent_developments = interpretation.get("recent_developments") or []

    event_parts: list[str] = []
    max_parts = 2
    for part in confirmed_facts[:max_parts]:
        _add_unique_text(event_parts, part)
    remaining = max_parts - len(event_parts)
    if realized_market_events:
        for part in realized_market_events[:remaining]:
            _add_unique_text(event_parts, part)
    else:
        for part in anticipated_consequences[:remaining]:
            _add_unique_text(event_parts, part)
    if not event_parts:
        for part in recent_developments[:2]:
            _add_unique_text(event_parts, part)
    return "; ".join(_capitalize_text(part) for part in event_parts if part)


def _prune_anticipated_consequences(
    confirmed_facts: list[str],
    anticipated_consequences: list[str],
    realized_market_events: list[str],
) -> list[str]:
    confirmed_low = " ".join(_text(item).lower() for item in confirmed_facts)
    realized_low = " ".join(_text(item).lower() for item in realized_market_events)
    out: list[str] = []
    for item in anticipated_consequences:
        low = _text(item).lower()
        if "talks are still ahead" in low and any(
            marker in confirmed_low for marker in ("talks are ongoing", "talks resumed", "talks failed", "talks ended")
        ):
            continue
        if "blockade risk" in low and "blockade was imposed" in realized_low:
            continue
        if "shipping disruption risk" in low and any(marker in realized_low for marker in ("shipping disrupted", "shipping halted")):
            continue
        out.append(item)
    return out


def _contains_any(low: str, needles) -> bool:
    return any(needle in low for needle in needles)


def _phrase_hits(low: str, phrases) -> list[str]:
    hits: list[str] = []
    for phrase in phrases:
        needle = _text(phrase).lower()
        if needle and needle in low and needle not in hits:
            hits.append(needle)
    return hits


def _entity_hits(low: str, entities) -> list[str]:
    hits: list[str] = []
    for entity in entities:
        needle = _text(entity).lower()
        if not needle:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", low) and needle not in hits:
            hits.append(needle)
    return hits


def _critical_topic(
    *,
    topic_id: str,
    severity_floor: str,
    event_bias: str,
    matched_entities: list[str],
    matched_phrases: list[str],
    escalation_reason: str,
) -> dict:
    severity = severity_floor if severity_floor in _VALID_CRITICAL_SEVERITIES else "medium"
    confirm_policy = "normal"
    if severity == "medium":
        confirm_policy = "defensive"
    elif severity in {"high", "severe"}:
        confirm_policy = "block_stale_confirm" if severity == "severe" else "defensive"
    return {
        "topic_id": topic_id,
        "severity_floor": severity,
        "event_bias": event_bias if event_bias in {"risk_off", "risk_on", "mixed", "unknown"} else "unknown",
        "headline_risk_active": True,
        "confirm_policy": confirm_policy,
        "matched_entities": _unique_text_tokens(matched_entities),
        "matched_phrases": _unique_text_tokens(matched_phrases),
        "escalation_reason": escalation_reason,
    }


def detect_critical_topics_from_text(text: str) -> list[dict]:
    low = _text(text).lower()
    if not low:
        return []

    topics: list[dict] = []

    geo_entities = _entity_hits(
        low,
        (
            "iran",
            "us",
            "u.s.",
            "trump",
            "israel",
            "lebanon",
            "kuwait",
            "gulf",
            "middle east",
            "hezbollah",
            "irgc",
            "houthis",
            "militia",
            "иран",
            "сша",
            "трамп",
            "натанз",
        ),
    )
    geo_phrases = _phrase_hits(
        low,
        (
            "halts talks",
            "halted talks",
            "suspends negotiations",
            "suspended negotiations",
            "talks failed",
            "peace talks fail",
            "ceasefire collapse",
            "war escalation",
            "war will work out",
            "struck iranian radar",
            "launches strikes",
            "launched strikes",
            "strikes against iran",
            "radar sites",
            "missile attack",
            "missile and drone attacks",
            "drone attack",
            "launches attacks",
            "launched attacks",
            "american military facilities",
            "fresh us strikes",
            "fresh u.s. strikes",
            "military base",
            "military bases",
            "airstrike",
            "ground offensive",
            "offensive in lebanon",
            "retaliation",
            "proxy forces",
            "conflict expands",
            "will strike iran",
            "strike iran very strongly",
            "strong strikes on iran",
            "strikes today",
            "strikes tomorrow",
            "attacks across gulf",
            "houthis fire missiles",
            "natanz",
            "nuclear site",
            "сша намерены нанести",
            "сильные удары по ирану",
            "сильный удар по ирану",
            "сегодня мы нанесем",
            "завтра тоже нанесем",
            "натанз",
            "ядерному объекту",
            "бомбардировке",
        ),
    )
    if geo_entities and geo_phrases:
        severe_markers = _phrase_hits(
            low,
            (
                "struck iranian radar",
                "launches strikes",
                "launched strikes",
                "strikes against iran",
                "missile and drone attacks",
                "launches attacks",
                "launched attacks",
                "american military facilities",
                "fresh us strikes",
                "fresh u.s. strikes",
                "military base",
                "military bases",
                "conflict expands",
                "war escalation",
                "ceasefire collapse",
                "ground offensive",
                "will strike iran",
                "strike iran very strongly",
                "strong strikes on iran",
                "strikes today",
                "strikes tomorrow",
                "attacks across gulf",
                "houthis fire missiles",
                "natanz",
                "nuclear site",
                "сша намерены нанести",
                "сильные удары по ирану",
                "сильный удар по ирану",
                "сегодня мы нанесем",
                "завтра тоже нанесем",
                "натанз",
                "ядерному объекту",
                "бомбардировке",
            ),
        )
        topics.append(
            _critical_topic(
                topic_id="geopolitical_military_escalation",
                severity_floor="severe" if severe_markers else "high",
                event_bias="risk_off",
                matched_entities=geo_entities,
                matched_phrases=geo_phrases,
                escalation_reason="military/diplomatic escalation cluster is active in market-moving headlines",
            )
        )

    shipping_entities = _entity_hits(
        low,
        (
            "strait of hormuz",
            "hormuz",
            "bab el-mandeb",
            "red sea",
            "suez canal",
            "persian gulf",
            "gulf",
            "ормуз",
            "персидский залив",
        ),
    )
    shipping_phrases = _phrase_hits(
        low,
        (
            "blockade",
            "closure",
            "threat to block",
            "threatens to block",
            "shipping disruption",
            "shipping route disruption",
            "tanker attacks",
            "oil tankers come under fire",
            "oil supply shock",
            "energy shock",
            "naval blockade",
            "blockade of iranian shipping",
            "entire iranian coastline",
            "all vessels",
            "control of the strait of hormuz",
            "блокад",
            "судоходств",
        ),
    )
    if shipping_entities and shipping_phrases:
        topics.append(
            _critical_topic(
                topic_id="strategic_shipping_energy_chokepoint",
                severity_floor="severe"
                if any(("blockade" in p) or ("блокад" in p) or p in {"closure", "threatens to block", "threat to block"} for p in shipping_phrases)
                else "high",
                event_bias="risk_off",
                matched_entities=shipping_entities,
                matched_phrases=shipping_phrases,
                escalation_reason="strategic shipping/energy chokepoint risk can reprice crypto beta abruptly",
            )
        )

    macro_entities = _entity_hits(low, ("fed", "federal reserve", "treasury", "cpi", "nfp", "fomc", "president"))
    macro_phrases = _phrase_hits(
        low,
        (
            "fed independence crisis",
            "fire fed officials",
            "fire officials over policy",
            "emergency fed",
            "unexpected rate policy shock",
            "cpi surprise",
            "nfp surprise",
            "fomc surprise",
            "capital controls",
            "debt crisis",
            "shutdown",
            "fiscal crisis",
        ),
    )
    if macro_entities and macro_phrases:
        topics.append(
            _critical_topic(
                topic_id="us_macro_policy_shock",
                severity_floor="high",
                event_bias="risk_off",
                matched_entities=macro_entities,
                matched_phrases=macro_phrases,
                escalation_reason="urgent US macro/policy shock can force cross-asset repricing",
            )
        )

    stablecoin_adoption = _is_stablecoin_adoption_story(low)
    crypto_entities = _entity_hits(low, ("btc", "bitcoin", "etf", "strategy", "stablecoin", "exchange", "sec", "ofac"))
    crypto_phrases = _phrase_hits(
        low,
        (
            "record outflows",
            "etf outflows hit a record",
            "strategy sells btc",
            "major issuer sells btc",
            "stablecoin depeg",
            "withdrawal halt",
            "halted withdrawals",
            "exchange outage",
            "liquidation cascade",
            "perp funding extreme",
            "oi unwind",
            "major hack",
            "bridge failure",
            "exploit",
            "major enforcement action",
        ),
    )
    if crypto_entities and crypto_phrases and not stablecoin_adoption:
        topics.append(
            _critical_topic(
                topic_id="crypto_market_structure_shock",
                severity_floor="severe" if any(p in crypto_phrases for p in ("stablecoin depeg", "liquidation cascade", "withdrawal halt", "halted withdrawals")) else "high",
                event_bias="risk_off",
                matched_entities=crypto_entities,
                matched_phrases=crypto_phrases,
                escalation_reason="crypto market-structure shock can dominate technical continuation",
            )
        )

    regulatory_entities = _entity_hits(low, ("ofac", "aml", "vasp", "sanctions", "exchange", "stablecoin", "bridge"))
    regulatory_phrases = _phrase_hits(
        low,
        (
            "sanctions on crypto flows",
            "seized crypto funds",
            "crypto ban",
            "emergency restrictions",
            "vasp rules",
            "liquidity",
            "exchange access",
            "aml action",
            "ofac action",
        ),
    )
    if regulatory_entities and regulatory_phrases and not stablecoin_adoption:
        topics.append(
            _critical_topic(
                topic_id="sanctions_sovereign_regulatory_shock",
                severity_floor="high",
                event_bias="risk_off",
                matched_entities=regulatory_entities,
                matched_phrases=regulatory_phrases,
                escalation_reason="sanctions/regulatory shock can affect liquidity or exchange access",
            )
        )

    return _merge_critical_topics(topics)


def _merge_critical_topics(topics: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for topic in topics:
        topic_id = _text(topic.get("topic_id"))
        if not topic_id:
            continue
        existing = merged.get(topic_id)
        if existing is None:
            merged[topic_id] = copy.deepcopy(topic)
            continue
        existing["severity_floor"] = _stronger_regime_severity(existing.get("severity_floor"), topic.get("severity_floor")) or existing.get("severity_floor")
        existing["confirm_policy"] = _stronger_confirm_policy(existing.get("confirm_policy"), topic.get("confirm_policy"))
        if existing.get("event_bias") != topic.get("event_bias"):
            existing["event_bias"] = "mixed" if "risk_off" not in {existing.get("event_bias"), topic.get("event_bias")} else "risk_off"
        existing["matched_entities"] = _unique_text_tokens([*(existing.get("matched_entities") or []), *(topic.get("matched_entities") or [])])
        existing["matched_phrases"] = _unique_text_tokens([*(existing.get("matched_phrases") or []), *(topic.get("matched_phrases") or [])])
        existing["escalation_reason"] = _merge_unique_texts(existing.get("escalation_reason"), topic.get("escalation_reason"), max_items=2)
    return sorted(merged.values(), key=lambda t: (_regime_severity_rank(t.get("severity_floor")), len(t.get("matched_phrases") or [])), reverse=True)


def _stronger_confirm_policy(*values) -> str:
    order = {"normal": 0, "defensive": 1, "block_stale_confirm": 2}
    best = "normal"
    for value in values:
        text = _text(value)
        if text in order and order[text] > order[best]:
            best = text
    return best


def _critical_topic_snapshot_fields(items: list[dict]) -> dict:
    topics: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        blob = " ".join(
            [
                _text(item.get("source_title")),
                _text(item.get("event")),
                _text(item.get("summary")),
                " ".join(item.get("drivers") or []),
                " ".join(item.get("confirmed_facts") or []),
                " ".join(item.get("anticipated_consequences") or []),
                " ".join(item.get("realized_market_events") or []),
                " ".join(item.get("recent_developments") or []),
            ]
        )
        topics.extend(detect_critical_topics_from_text(blob))
    topics = _merge_critical_topics(topics)
    dominant = topics[0] if topics else None
    event_bias = "neutral"
    if any(t.get("event_bias") == "risk_off" for t in topics):
        event_bias = "risk_off"
    elif any(t.get("event_bias") == "risk_on" for t in topics):
        event_bias = "risk_on"
    elif any(t.get("event_bias") in {"mixed", "unknown"} for t in topics):
        event_bias = "mixed"
    severity = _text((dominant or {}).get("severity_floor"))
    return {
        "critical_topics": topics,
        "dominant_critical_topic": copy.deepcopy(dominant) if dominant else None,
        "headline_risk_active": bool(topics),
        "event_risk_level": severity if severity in {"medium", "high", "severe"} else "low",
        "event_bias": event_bias,
        "confirm_policy": _stronger_confirm_policy(*(t.get("confirm_policy") for t in topics)) if topics else "normal",
        "matched_entities": _unique_text_tokens([entity for topic in topics for entity in (topic.get("matched_entities") or [])]),
        "matched_phrases": _unique_text_tokens([phrase for topic in topics for phrase in (topic.get("matched_phrases") or [])]),
        "escalation_reason": _merge_unique_texts(*(topic.get("escalation_reason") for topic in topics), max_items=2) if topics else "",
    }


def _headline_state_markers(text: str) -> set[str]:
    low = _text(text).lower()
    markers: set[str] = set()
    if any(marker in low for marker in _HEADLINE_MILITARY_ESCALATION_MARKERS):
        markers.add("military_action")
    if any(marker in low for marker in _HEADLINE_SHIPPING_ENERGY_ESCALATION_MARKERS):
        markers.add("shipping_energy_chokepoint")
    if any(marker in low for marker in _HEADLINE_DEESCALATION_MARKERS):
        markers.add("deescalation")
    if any(marker in low for marker in ("risk_off", "downside volatility", "tail risk", "risk premium")):
        markers.add("market_relevance")
    return markers


def _headline_transition_reason(previous_text: str, new_text: str, delta: str) -> str:
    new_markers = _headline_state_markers(new_text)
    previous_markers = _headline_state_markers(previous_text)
    added = new_markers - previous_markers
    if "military_action" in added or (delta == "ESCALATION" and "military_action" in new_markers):
        return "military_action"
    if "shipping_energy_chokepoint" in added or (delta == "ESCALATION" and "shipping_energy_chokepoint" in new_markers):
        return "shipping_energy_risk"
    if "deescalation" in added or (delta == "DEESCALATION" and "deescalation" in new_markers):
        return "deescalation_confirmed"
    if delta == "MINOR_UPDATE":
        return "same_topic_minor_update"
    return "same_topic_same_state"


def _headline_delta_rank(value) -> int:
    return {"NONE": 0, "MINOR_UPDATE": 1, "DEESCALATION": 2, "ESCALATION": 3}.get(_text(value), 0)


def _dominant_headline_delta(items: list[dict]) -> dict:
    best = None
    for item in items:
        if best is None or _headline_delta_rank(item.get("headline_risk_delta")) > _headline_delta_rank(best.get("headline_risk_delta")):
            best = item
    if not best:
        return {
            "headline_risk_delta": "NONE",
            "previous_risk_level": "",
            "new_risk_level": "",
            "risk_transition_reason": "",
            "duplicate_decision": "duplicate_headline_same_state",
            "headline_update_generated": False,
        }
    return {
        "headline_risk_delta": best.get("headline_risk_delta") or "NONE",
        "previous_risk_level": best.get("previous_risk_level") or "",
        "new_risk_level": best.get("new_risk_level") or "",
        "risk_transition_reason": best.get("risk_transition_reason") or "",
        "duplicate_decision": best.get("duplicate_decision") or "duplicate_headline_same_state",
        "headline_update_generated": bool(best.get("headline_update_generated")),
    }


def _snapshot_headline_delta(items: list[dict], critical_fields: dict, dominant_delta: dict) -> dict:
    if _headline_delta_rank(dominant_delta.get("headline_risk_delta")) >= _headline_delta_rank("ESCALATION"):
        return dominant_delta
    if critical_fields.get("event_risk_level") != "severe" or critical_fields.get("event_bias") != "risk_off":
        return dominant_delta
    if len(items) < 2:
        return dominant_delta
    combined = " ".join(
        " ".join(
            [
                _text(item.get("source_title")),
                _text(item.get("event")),
                " ".join(item.get("recent_developments") or []),
                " ".join(item.get("confirmed_facts") or []),
                " ".join(item.get("anticipated_consequences") or []),
            ]
        )
        for item in items
    )
    markers = _headline_state_markers(combined)
    if "military_action" in markers:
        return {
            "headline_risk_delta": "ESCALATION",
            "previous_risk_level": critical_fields.get("event_risk_level") or dominant_delta.get("previous_risk_level") or "",
            "new_risk_level": critical_fields.get("event_risk_level") or dominant_delta.get("new_risk_level") or "",
            "risk_transition_reason": "military_action",
            "duplicate_decision": "headline_risk_update",
            "headline_update_generated": True,
        }
    if "deescalation" in markers:
        return {
            "headline_risk_delta": "DEESCALATION",
            "previous_risk_level": critical_fields.get("event_risk_level") or dominant_delta.get("previous_risk_level") or "",
            "new_risk_level": dominant_delta.get("new_risk_level") or "",
            "risk_transition_reason": "deescalation_confirmed",
            "duplicate_decision": "headline_risk_update",
            "headline_update_generated": False,
        }
    return dominant_delta


def _headline_risk_delta_for_cluster(item: dict, interpretation: dict, *, impact: str, directional_risk: str) -> dict:
    source_titles = [_text(title) for title in (item.get("_source_titles") or []) if _text(title)]
    developments = [_text(value) for value in (interpretation.get("recent_developments") or []) if _text(value)]
    previous_text = source_titles[0] if source_titles else (developments[0] if developments else "")
    latest_text = source_titles[-1] if source_titles else (developments[-1] if developments else previous_text)
    previous_impact = _classify_impact(previous_text, category=item.get("category"), phase=_classify_phase(previous_text, category=item.get("category")))
    previous_interpretation = _interpret_event_title(previous_text, category=item.get("category")) if previous_text else {}
    previous_phase = _classify_phase(previous_text, category=item.get("category"), interpretation=previous_interpretation)
    previous_direction = _classify_directional_risk(previous_text, phase=previous_phase, interpretation=previous_interpretation)

    previous_markers = _headline_state_markers(previous_text)
    latest_markers = _headline_state_markers(latest_text)
    added_markers = latest_markers - previous_markers
    impact_up = _impact_rank(impact) > _impact_rank(previous_impact)
    direction_up = previous_direction != "risk_off" and directional_risk == "risk_off"
    direction_down = previous_direction == "risk_off" and directional_risk == "risk_on"

    delta = "NONE"
    if "deescalation" in added_markers or direction_down:
        delta = "DEESCALATION"
    elif added_markers & {"military_action", "shipping_energy_chokepoint"} or impact_up or direction_up:
        delta = "ESCALATION"
    elif len(source_titles) > 1 or len(developments) > 1:
        delta = "MINOR_UPDATE"

    duplicate_decision = "duplicate_headline_same_state" if delta in {"NONE", "MINOR_UPDATE"} else "headline_risk_update"
    return {
        "headline_risk_delta": delta,
        "previous_risk_level": _stronger_regime_severity(previous_impact) or previous_impact or "",
        "new_risk_level": _stronger_regime_severity(impact) or impact or "",
        "risk_transition_reason": _headline_transition_reason(previous_text, latest_text, delta),
        "duplicate_decision": duplicate_decision,
        "headline_update_generated": delta == "ESCALATION",
    }


def _capitalize_text(value: str) -> str:
    text = _text(value)
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _add_unique_text(target: list[str], value: str) -> None:
    text = _text(value)
    if not text:
        return
    key = _topic_key(text)
    if not key:
        return
    if any(_topic_key(existing) == key for existing in target):
        return
    target.append(text)


def _has_talks_context(low: str) -> bool:
    return any(
        marker in low
        for marker in (
            "talks",
            "peace talks",
            "negotiation",
            "negotiations",
            "meeting",
            "summit",
            "ceasefire",
            "truce",
            "press conference",
            "briefing",
            "remarks",
            "white house",
            "oval office",
        )
    )


def _geopolitics_topic_prefix(low: str) -> str:
    if "israel" in low and ("lebanon" in low or "hezbollah" in low):
        return "Israel-Lebanon"
    if "u.s.-iran" in low or "us-iran" in low or (("iran" in low) and ("u.s." in low or "us " in low or "trump" in low)):
        return "US-Iran"
    if "iran" in low:
        return "Iran"
    if "hormuz" in low or "strait of hormuz" in low:
        return "Hormuz"
    return ""


def _interpret_geopolitics_title(title: str) -> dict:
    low = _text(title).lower()
    confirmed_facts: list[str] = []
    anticipated_consequences: list[str] = []
    realized_market_events: list[str] = []
    topic_prefix = _geopolitics_topic_prefix(low)
    talks_label = f"{topic_prefix} talks".strip() if topic_prefix and "Hormuz" not in topic_prefix else "Talks"
    talks_label = talks_label if _has_talks_context(low) else "Talks"

    if _contains_any(low, _TALKS_FAILED_MARKERS):
        _add_unique_text(confirmed_facts, f"{talks_label} failed")
    elif "talks ended" in low or "negotiations ended" in low:
        _add_unique_text(confirmed_facts, f"{talks_label} ended")

    if _contains_any(low, ("talks resumed", "resumes talks", "resume talks", "talks resumed")):
        _add_unique_text(confirmed_facts, f"{talks_label} resumed")

    if _contains_any(low, _TALKS_ONGOING_MARKERS) and _has_talks_context(low):
        _add_unique_text(confirmed_facts, f"{talks_label} are ongoing")

    if _contains_any(low, _TALKS_UPCOMING_MARKERS) and _has_talks_context(low):
        _add_unique_text(anticipated_consequences, f"{talks_label} are still ahead")

    if any(marker in low for marker in ("ceasefire", "truce")) and any(
        marker in low
        for marker in (
            "ceasefire extended",
            "truce extended",
            "ceasefire extension",
            "truce extension",
            "extended by three weeks",
            "extended by 3 weeks",
        )
    ):
        phrase = f"{topic_prefix} ceasefire was extended".strip() if topic_prefix else "Ceasefire was extended"
        _add_unique_text(confirmed_facts, phrase)
        _add_unique_text(realized_market_events, phrase)

    if any(marker in low for marker in ("ceasefire", "truce")) and any(
        marker in low for marker in ("deadline", "due to expire", "set to expire", "extension possible")
    ):
        phrase = f"{topic_prefix} ceasefire deadline is near".strip() if topic_prefix else "Ceasefire deadline is near"
        _add_unique_text(anticipated_consequences, phrase)

    if "sanctions" in low and ("announc" in low or "imposed" in low):
        _add_unique_text(confirmed_facts, "Sanctions were announced")
        _add_unique_text(realized_market_events, "Sanctions were announced")

    if "tariff" in low and ("announc" in low or "imposed" in low):
        _add_unique_text(confirmed_facts, "Tariffs were announced")
        _add_unique_text(realized_market_events, "Tariffs were announced")

    if "blockade" in low:
        if _contains_any(low, ("imposed", "in force", "implemented", "takes effect", "confirmed")):
            phrase = "Hormuz blockade was imposed" if "hormuz" in low or "strait of hormuz" in low else "Blockade was imposed"
            _add_unique_text(confirmed_facts, phrase)
            _add_unique_text(realized_market_events, phrase)
        elif _contains_any(low, ("announc", "threat", "warn", "consider", "plan", "float", "signal", "order")):
            phrase = "Hormuz blockade risk increased" if "hormuz" in low or "strait of hormuz" in low else "Blockade risk increased"
            _add_unique_text(anticipated_consequences, phrase)
        elif _contains_any(low, _ESCALATION_RISK_MARKERS):
            phrase = "Hormuz blockade risk remains elevated" if "hormuz" in low or "strait of hormuz" in low else "Blockade risk remains elevated"
            _add_unique_text(anticipated_consequences, phrase)
        elif _contains_any(low, _MIXED_TRANSITION_MARKERS) and confirmed_facts:
            phrase = "Hormuz blockade risk increased" if "hormuz" in low or "strait of hormuz" in low else "Blockade risk increased"
            _add_unique_text(anticipated_consequences, phrase)

    if _contains_any(low, ("strike confirmed", "strike launched", "missile strike", "attack confirmed", "attacks launched")):
        _add_unique_text(confirmed_facts, "Military strike was confirmed")
        _add_unique_text(realized_market_events, "Military strike was confirmed")

    if "retaliation" in low and not realized_market_events:
        phrase = "Retaliation risk increased"
        if _contains_any(low, ("confirmed", "launched", "began")):
            phrase = "Retaliation was confirmed"
            _add_unique_text(realized_market_events, phrase)
        _add_unique_text(confirmed_facts if phrase.endswith("confirmed") else anticipated_consequences, phrase)

    if "shipping" in low and not _contains_any(low, ("shipping halted", "shipping disrupted", "resume shipping", "reopened")):
        if _contains_any(low, ("risk", "threat", "in focus", "under threat", "risk stays", "risk remains")):
            _add_unique_text(anticipated_consequences, "Shipping disruption risk remains elevated")
        elif confirmed_facts and _contains_any(low, _MIXED_TRANSITION_MARKERS):
            _add_unique_text(anticipated_consequences, "Shipping disruption risk remains elevated")

    if "ceasefire" in low and _contains_any(low, ("may collapse", "could collapse", "fragile", "at risk")):
        _add_unique_text(anticipated_consequences, "Ceasefire collapse risk increased")
    if any(marker in low for marker in ("ceasefire", "truce")) and _contains_any(
        low,
        ("life support", "massive life support", "frays", "fraying"),
    ):
        _add_unique_text(anticipated_consequences, "Ceasefire collapse risk increased")

    if "escalation" in low and _contains_any(low, _ESCALATION_RISK_MARKERS):
        _add_unique_text(anticipated_consequences, "Escalation risk remains elevated")
    if "retaliation" in low and confirmed_facts and not realized_market_events and not anticipated_consequences:
        _add_unique_text(anticipated_consequences, "Retaliation risk increased")

    mixed_unresolved = bool(confirmed_facts and anticipated_consequences and not realized_market_events)
    interpretation = {
        "confirmed_facts": confirmed_facts,
        "anticipated_consequences": anticipated_consequences,
        "realized_market_events": realized_market_events,
        "mixed_unresolved": mixed_unresolved,
    }
    event = _event_label_from_interpretation(interpretation)
    recent_developments = [event] if event else ([_event_label(title)] if _event_label(title) else [])

    return {
        "confirmed_facts": confirmed_facts,
        "anticipated_consequences": anticipated_consequences,
        "realized_market_events": realized_market_events,
        "mixed_unresolved": mixed_unresolved,
        "recent_developments": recent_developments,
        "event": event,
    }


def _interpret_event_title(title: str, *, category: str) -> dict:
    if category == "geopolitics":
        return _interpret_geopolitics_title(title)
    return {
        "confirmed_facts": [],
        "anticipated_consequences": [],
        "realized_market_events": [],
        "mixed_unresolved": False,
        "event": "",
    }


def _is_scheduled_macro_title(title: str, calendar_events=None) -> bool:
    low = _text(title).lower()
    if not low:
        return False
    if any(needle in low for needle in _UNSCHEDULED_OVERRIDE_NEEDLES):
        return False
    if any(needle in low for needle in _SCHEDULED_EVENT_NEEDLES):
        return True
    if not isinstance(calendar_events, list):
        return False
    title_key = _topic_key(title)
    for event in calendar_events:
        if not isinstance(event, dict):
            continue
        event_key = _topic_key(event.get("event"))
        if event_key and (event_key == title_key or event_key in title_key or title_key in event_key):
            return True
    return False


def _classify_category(title: str) -> str:
    low = _text(title).lower()
    critical_topic_ids = {
        _text(topic.get("topic_id"))
        for topic in detect_critical_topics_from_text(title)
        if isinstance(topic, dict)
    }
    if critical_topic_ids & {"geopolitical_military_escalation", "strategic_shipping_energy_chokepoint"}:
        return "geopolitics"
    relevance = classify_market_relevance(title)
    relevance_category = _text(relevance.get("category"))

    if _is_stablecoin_adoption_story(low):
        return "crypto_market_structure"

    if any(needle in low for needle in _CRYPTO_MARKET_STRUCTURE_NEEDLES) and any(
        needle in low for needle in _CRYPTO_SHOCK_NEEDLES
    ) and relevance_category == "crypto":
        return "crypto_market_structure"
    if any(needle in low for needle in _GEOPOLITICS_NEEDLES) and relevance_category in {
        "geopolitics",
        "energy_shipping",
    }:
        return "geopolitics"
    if any(needle in low for needle in _MACRO_POLICY_NEEDLES) and relevance_category in {"macro", "banking"}:
        return "macro_policy_shock"
    if any(needle in low for needle in _CRYPTO_MARKET_STRUCTURE_NEEDLES) and relevance_category == "crypto":
        return "crypto_market_structure"
    return ""


def _classify_phase(title: str, *, category: str, interpretation: dict | None = None) -> str:
    low = _text(title).lower()
    interpretation = interpretation or {}
    realized_market_events = interpretation.get("realized_market_events") or []
    anticipated_consequences = interpretation.get("anticipated_consequences") or []
    confirmed_facts = interpretation.get("confirmed_facts") or []

    if category == "geopolitics":
        if realized_market_events:
            return "post_event"
        if anticipated_consequences:
            if any("ongoing" in fact.lower() or "resumed" in fact.lower() for fact in confirmed_facts):
                return "ongoing"
            return "pre_event"
    if any(marker in low for marker in _POST_EVENT_MARKERS):
        return "post_event"
    if any(marker in low for marker in _ONGOING_MARKERS):
        return "ongoing"
    if any(marker in low for marker in _PRE_EVENT_MARKERS):
        return "pre_event"
    if category == "geopolitics" and any(
        marker in low for marker in ("talks", "negotiation", "negotiations", "summit", "meeting", "ceasefire")
    ):
        if any(marker in low for marker in ("begin", "begins", "continue", "continues", "resume", "resumes", "underway", "under way")):
            return "ongoing"
        return "pre_event"
    return "post_event"


def _classify_impact(title: str, *, category: str, phase: str) -> str:
    low = _text(title).lower()
    if category == "crypto_market_structure" and _is_stablecoin_adoption_story(low):
        return "medium"
    if category == "geopolitics" and any(
        needle in low
        for needle in (
            "hormuz",
            "blockade",
            "shipping",
            "strike",
            "missile",
            "military",
            "sanctions",
            "tariff",
            "terror",
            "terrorism",
        )
    ):
        return "high"
    if category == "crypto_market_structure" and any(
        needle in low
        for needle in (
            "liquidation",
            "outage",
            "hack",
            "exploit",
            "etf",
            "sec",
            "lawsuit",
        )
    ):
        return "high"
    if category == "geopolitics" and phase in {"pre_event", "ongoing"}:
        return "high"
    if category == "macro_policy_shock":
        return "high"
    return "medium"


def _classify_directional_risk(title: str, *, phase: str, interpretation: dict | None = None) -> str:
    low = _text(title).lower()
    interpretation = interpretation or {}
    anticipated_consequences = interpretation.get("anticipated_consequences") or []
    realized_market_events = interpretation.get("realized_market_events") or []

    if _is_stablecoin_adoption_story(low):
        return "mixed"

    if any(marker in low for marker in _HEADLINE_DEESCALATION_MARKERS):
        return "risk_on"

    if phase in {"pre_event", "ongoing"} and anticipated_consequences:
        return "uncertain"
    if phase == "post_event" and realized_market_events and not any(needle in low for needle in _RISK_ON_NEEDLES):
        return "risk_off"
    if phase in {"pre_event", "ongoing"} and any(
        needle in low for needle in ("talks", "negotiation", "meeting", "summit", "ceasefire")
    ):
        return "uncertain"
    if any(needle in low for needle in _RISK_ON_NEEDLES):
        return "risk_on"
    if any(needle in low for needle in _RISK_OFF_NEEDLES):
        return "risk_off"
    if phase in {"pre_event", "ongoing"}:
        return "uncertain"
    return "mixed"


def _time_text_for_phase(phase: str, interpretation: dict | None = None) -> str:
    interpretation = interpretation or {}
    confirmed_facts = interpretation.get("confirmed_facts") or []
    realized_market_events = interpretation.get("realized_market_events") or []
    if any("ongoing" in fact.lower() or "resumed" in fact.lower() for fact in confirmed_facts):
        return "ongoing"
    if realized_market_events or confirmed_facts:
        return "recent"
    if phase == "pre_event":
        return "expected"
    if phase == "ongoing":
        return "ongoing"
    return "recent"


def _event_label(title: str) -> str:
    text = _text(title)
    if not text:
        return ""
    text = re.sub(r"\s+—\s+https?://\S+.*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160].rstrip()


def _summary_unresolved_phrase(consequence: str) -> str:
    text = _text(consequence)
    if not text:
        return "следующий шаг эскалации пока не подтверждён"
    if " risk " in f" {text.lower()} ":
        if text.lower().endswith("risk increased"):
            return f"{text[:-len('risk increased')].strip()} пока не подтверждён".replace("  ", " ")
        if text.lower().endswith("risk remains elevated"):
            return f"{text[:-len('risk remains elevated')].strip()} пока не подтверждён".replace("  ", " ")
        if text.lower().endswith("risk remains"):
            return f"{text[:-len('risk remains')].strip()} пока не подтверждён".replace("  ", " ")
        if text.lower().endswith("risk stays"):
            return f"{text[:-len('risk stays')].strip()} пока не подтверждён".replace("  ", " ")
    if text.lower().endswith("are still ahead"):
        return text.replace("are still ahead", "ещё впереди")
    return f"{text} остаётся нерешённым"


def _stablecoin_adoption_narrative() -> str:
    return (
        "отражает рост использования стейблкоинов и поддерживает тему крипто-ликвидности, "
        "но не является самостоятельным шоковым драйвером рынка."
    )


def _execution_tail_text(category: str, *, phase: str, mixed_unresolved: bool) -> str:
    if category == "geopolitics":
        if mixed_unresolved:
            return "Чувствительность к заголовкам и волатильность остаются повышенными."
        if phase in {"pre_event", "ongoing"}:
            return "До прояснения картины чувствительность к заголовкам остаётся повышенной."
        return "Премия за риск остаётся высокой, пока follow-through не станет яснее."
    if category == "crypto_market_structure":
        return "Риск исполнения остаётся повышенным, пока не пройдут вынужденные потоки."
    return "Кросс-активная волатильность может оставаться повышенной, пока follow-through не станет яснее."


def _event_drivers(
    title: str,
    *,
    category: str,
    phase: str,
    directional_risk: str,
    interpretation: dict | None = None,
) -> list[str]:
    low = _text(title).lower()
    interpretation = interpretation or {}
    confirmed_facts = interpretation.get("confirmed_facts") or []
    anticipated_consequences = interpretation.get("anticipated_consequences") or []
    mixed_unresolved = bool(interpretation.get("mixed_unresolved"))
    drivers: list[str] = []

    if category == "crypto_market_structure" and _is_stablecoin_adoption_story(low):
        drivers.extend(
            [
                "headline reflects broader stablecoin usage rather than a market shock",
                "it supports the crypto-liquidity theme in the background",
                "price action still needs independent technical confirmation",
            ]
        )
        return _merge_unique_drivers(drivers, max_items=4)

    if category == "geopolitics":
        if mixed_unresolved:
            drivers.extend(
                [
                    "confirmed diplomatic developments already changed the backdrop",
                    "the main escalation step is still unresolved",
                    "headline sensitivity stays elevated while tail risk is repriced",
                ]
            )
        elif phase == "pre_event":
            drivers.extend(
                [
                    "talks or diplomatic headlines are known ahead of resolution",
                    "headline sensitivity is elevated before clarity",
                    "outcome remains uncertain",
                ]
            )
        elif phase == "ongoing":
            drivers.extend(
                [
                    "negotiations are still developing",
                    "the market is waiting for confirmation rather than headlines alone",
                    "volatility can stay elevated while the process is unresolved",
                ]
            )
        else:
            if directional_risk == "risk_on":
                drivers.extend(
                    [
                        "de-escalation headline supports short-term relief",
                        "risk premium can compress if follow-through holds",
                        "markets may still demand confirmation after the first move",
                    ]
                )
            else:
                drivers.extend(
                    [
                        "de-escalation is not confirmed",
                        "geopolitical risk premium remains in play",
                        "headline-driven volatility can stay elevated",
                    ]
                )
        if confirmed_facts and any("failed" in fact.lower() for fact in confirmed_facts):
            drivers.append("a failed diplomatic path keeps de-escalation hopes weaker")
        if anticipated_consequences:
            drivers.append("the next geopolitical catalyst remains path-dependent")
        if "hormuz" in low or "shipping" in low or "blockade" in low:
            drivers.append("shipping and energy tail risk stays market-sensitive")

    if category == "macro_policy_shock":
        drivers.extend(
            [
                "unexpected policy language can force fast cross-asset repricing",
                "rates, dollar and beta sentiment can shift abruptly",
                "headline shock matters more than clean trend continuation",
            ]
        )
        if "tariff" in low or "sanctions" in low:
            drivers.append("growth and inflation expectations can be repriced together")

    if category == "crypto_market_structure":
        if "liquidation" in low:
            drivers.extend(
                [
                    "forced positioning unwind can amplify overshoots",
                    "thin liquidity can distort price discovery",
                    "better to wait for structure to stabilize before chasing",
                ]
            )
        elif any(needle in low for needle in ("outage", "halt", "withdrawals", "exchange", "hack", "exploit", "breach")):
            drivers.extend(
                [
                    "execution quality can deteriorate around operational stress",
                    "spread and liquidity risk can widen quickly",
                    "headline-led dislocations can overpower clean technical entries",
                ]
            )
        else:
            drivers.extend(
                [
                    "headline shock can reprice sector risk quickly",
                    "positioning can swing faster than normal trend signals",
                    "confirmation matters more than first-reaction momentum",
                ]
            )

    return _merge_unique_drivers(drivers, max_items=4)


def _event_summary(
    title: str,
    *,
    category: str,
    phase: str,
    directional_risk: str,
    interpretation: dict | None = None,
) -> str:
    interpretation = interpretation or {}
    confirmed_facts = interpretation.get("confirmed_facts") or []
    anticipated_consequences = interpretation.get("anticipated_consequences") or []
    mixed_unresolved = bool(interpretation.get("mixed_unresolved"))
    label = interpretation.get("event") or _event_label(title) or "This catalyst"

    if category == "crypto_market_structure" and _is_stablecoin_adoption_story(title):
        return f"{label} {_stablecoin_adoption_narrative()}"

    if mixed_unresolved and confirmed_facts and anticipated_consequences:
        fact = _capitalize_text(confirmed_facts[0])
        unresolved = _summary_unresolved_phrase(anticipated_consequences[0])
        return f"{fact}, but {unresolved}. {_execution_tail_text(category, phase=phase, mixed_unresolved=True)}"

    if phase == "pre_event":
        if anticipated_consequences:
            return (
                f"{_capitalize_text(anticipated_consequences[0])}. "
                f"{_execution_tail_text(category, phase=phase, mixed_unresolved=False)}"
            )
        return (
            f"{label} ещё впереди, и до прояснения исхода чувствительность к заголовкам остаётся повышенной."
        )
    if phase == "ongoing":
        if confirmed_facts and any("ongoing" in fact.lower() or "resumed" in fact.lower() for fact in confirmed_facts):
            return "Переговоры продолжаются, рынок всё ещё ждёт ясности, а price action остаётся зависимым от заголовков."
        return (
            f"{label} остаётся нерешённым, поэтому рынок сохраняет чувствительность к заголовкам, "
            "а confirm важнее агрессивной погони."
        )
    if directional_risk == "risk_on":
        return (
            f"{label} поддерживает краткосрочный relief, но до подтверждения движения follow-through "
            "остаётся чувствительным к заголовкам."
        )
    if category == "crypto_market_structure":
        return (
            f"{label} повышает немедленный риск исполнения и может держать price action хаотичным, "
            "пока не выйдут принудительные потоки."
        )
    return (
        f"{label} удерживает повышенную премию за риск и усиливает волатильность на заголовках, "
        "пока рынок не получит более ясный follow-through."
    )


def _event_risk_sort_key(item: dict) -> tuple[int, int, int, int]:
    category = _text(item.get("category"))
    phase = _text(item.get("phase"))
    impact = _text(item.get("impact"))
    event_text = " ".join(
        [
            _text(item.get("event")),
            " ".join(item.get("confirmed_facts") or []),
            " ".join(item.get("anticipated_consequences") or []),
            " ".join(item.get("drivers") or []),
        ]
    ).lower()
    category_score = {
        "geopolitics": 90,
        "macro_policy_shock": 80,
        "crypto_market_structure": 45,
    }.get(category, 0)
    impact_score = {"high": 30, "medium": 15, "low": 0}.get(impact, 0)
    phase_score = {"pre_event": 10, "ongoing": 8, "post_event": 6}.get(phase, 0)
    market_scope_score = 0
    if any(needle in event_text for needle in ("hormuz", "blockade", "shipping", "sanctions", "tariff", "strike", "attack")):
        market_scope_score += 25
    if any(needle in event_text for needle in ("sec", "lawsuit", "etf", "regulatory")):
        market_scope_score -= 10
    return (category_score + impact_score + phase_score + market_scope_score, category_score, impact_score, market_scope_score)


def _geopolitical_regime_profile(item: dict | None) -> dict:
    default = {
        "regime_flags": [],
        "regime_severity": "",
        "regime_summary": "",
        "risk_asymmetry": "",
        "continuation_mode": "",
        "strictness": "",
    }
    if not isinstance(item, dict) or _text(item.get("category")) != "geopolitics":
        return copy.deepcopy(default)

    phase = _text(item.get("phase")).lower()
    impact = _text(item.get("impact")).lower()
    directional_risk = _text(item.get("directional_risk")).lower()
    blob = " ".join(
        [
            _text(item.get("source_title")),
            _text(item.get("event")),
            _text(item.get("summary")),
            " ".join(item.get("drivers") or []),
            " ".join(item.get("confirmed_facts") or []),
            " ".join(item.get("anticipated_consequences") or []),
            " ".join(item.get("realized_market_events") or []),
            " ".join(item.get("recent_developments") or []),
            " ".join(item.get("cluster_themes") or []),
        ]
    ).lower()
    confirmed_facts = item.get("confirmed_facts") or []
    anticipated_consequences = item.get("anticipated_consequences") or []
    mixed_unresolved = bool(confirmed_facts and anticipated_consequences and not (item.get("realized_market_events") or []))

    flags: list[str] = []
    if _contains_any(blob, _GEO_CEASEFIRE_RISK_MARKERS):
        flags.append("ceasefire_at_risk")
    if _contains_any(blob, _GEO_DIPLOMATIC_BREAKDOWN_MARKERS):
        flags.append("diplomatic_breakdown_risk")
    if _contains_any(blob, _GEO_MILITARY_THREAT_MARKERS):
        flags.append("renewed_military_action_threat")
    if phase in {"pre_event", "ongoing"} and _contains_any(blob, _GEO_NEGOTIATION_MARKERS):
        flags.append("unresolved_high_stakes_negotiation")
    if _contains_any(blob, _GEO_ESCALATION_SIGNAL_MARKERS):
        flags.append("public_escalation_signal")
    downside_blob = directional_risk in {"risk_off", "uncertain"} and _contains_any(blob, _GEO_DOWNSIDE_SHOCK_MARKERS)
    if downside_blob or "headline-driven volatility" in blob or "risk premium" in blob or "tail risk" in blob:
        flags.append("downside_shock_elevated")
    if mixed_unresolved or (
        phase in {"pre_event", "ongoing"}
        and (
            "diplomatic_breakdown_risk" in flags
            or "ceasefire_at_risk" in flags
            or "renewed_military_action_threat" in flags
        )
    ):
        flags.append("unresolved_geopolitical_breakpoint")
    if (
        len(flags) >= 2
        or "unresolved_geopolitical_breakpoint" in flags
        or "renewed_military_action_threat" in flags
    ):
        flags.append("escalation_sensitive")

    flags = _unique_text_tokens(flags)

    score = 0
    if impact == "high":
        score += 2
    if phase in {"pre_event", "ongoing"}:
        score += 2
    if mixed_unresolved:
        score += 2
    if "ceasefire_at_risk" in flags:
        score += 2
    if "diplomatic_breakdown_risk" in flags:
        score += 2
    if "renewed_military_action_threat" in flags:
        score += 2
    if "unresolved_high_stakes_negotiation" in flags:
        score += 1
    if "public_escalation_signal" in flags:
        score += 1
    if "downside_shock_elevated" in flags:
        score += 2
    if "unresolved_geopolitical_breakpoint" in flags:
        score += 2

    core_bundle_count = sum(
        1
        for key in (
            "ceasefire_at_risk",
            "diplomatic_breakdown_risk",
            "renewed_military_action_threat",
            "unresolved_high_stakes_negotiation",
            "public_escalation_signal",
            "downside_shock_elevated",
        )
        if key in flags
    )
    severity = "low"
    if (
        score >= 11
        and len(flags) >= 4
        and (
            core_bundle_count >= 4
            or "ceasefire_at_risk" in flags
            or "renewed_military_action_threat" in flags
            or "public_escalation_signal" in flags
        )
    ):
        severity = "severe"
    elif score >= 7:
        severity = "high"
    elif score >= 4:
        severity = "medium"

    risk_asymmetry = "neutral"
    if "downside_shock_elevated" in flags and severity in {"high", "severe"}:
        risk_asymmetry = "asymmetric_downside" if severity == "severe" else "downside_elevated"

    if (
        severity in {"high", "severe"}
        or (
            severity == "medium"
            and (
                "ceasefire_at_risk" in flags
                or "diplomatic_breakdown_risk" in flags
                or "unresolved_geopolitical_breakpoint" in flags
            )
        )
    ):
        flags.append("fragile_regime")
    if severity in {"medium", "high", "severe"} and (
        "escalation_sensitive" in flags
        or "fragile_regime" in flags
        or "unresolved_geopolitical_breakpoint" in flags
    ):
        flags.append("continuation_unstable")
    if severity in {"high", "severe"} and (
        "ceasefire_at_risk" in flags
        or "public_escalation_signal" in flags
        or "diplomatic_breakdown_risk" in flags
    ):
        flags.append("risk_of_sharp_regime_flip")
    if severity in {"high", "severe"} and (
        risk_asymmetry != "neutral"
        or "ceasefire_at_risk" in flags
        or "diplomatic_breakdown_risk" in flags
    ):
        flags.append("asymmetric_headline_risk")

    flags = _unique_text_tokens(flags)

    continuation_mode = "tactical_only" if severity == "severe" else "confirmation_first" if severity == "high" else "normal"
    strictness = "strict" if severity == "severe" else "elevated" if severity == "high" else "normal"

    if severity == "severe":
        summary = (
            "severe geopolitical regime: escalation-sensitive, two-sided with asymmetric downside shock risk; "
            "continuation is tactical-only until the resolution path is cleaner."
        )
    elif severity == "high":
        summary = (
            "elevated geopolitical regime: downside shock sensitivity is above normal and confirmation matters "
            "more than first-move continuation."
        )
    elif severity == "medium":
        summary = "geopolitical backdrop remains escalation-sensitive and can destabilize continuation if headlines worsen."
    else:
        summary = "geopolitical risk remains a live but secondary contextual factor."

    return {
        "regime_flags": flags,
        "regime_severity": severity,
        "regime_summary": summary,
        "risk_asymmetry": risk_asymmetry,
        "continuation_mode": continuation_mode,
        "strictness": strictness,
    }


def _build_event_risk_regime_layer(items: list[dict]) -> dict:
    geo_items = [item for item in items if isinstance(item, dict) and _text(item.get("category")) == "geopolitics"]
    if not geo_items:
        return {}

    dominant = geo_items[0]
    severity = ""
    flags: list[str] = []
    for item in geo_items:
        profile = _geopolitical_regime_profile(item)
        severity = _stronger_regime_severity(severity, profile.get("regime_severity"))
        flags = _unique_text_tokens([*flags, *(profile.get("regime_flags") or [])])

    dominant_profile = _geopolitical_regime_profile(dominant)
    if len(geo_items) >= 2 and severity == "high" and len(flags) >= 4:
        severity = "severe"
    if severity == "severe":
        strictness = "strict"
        continuation_mode = "tactical_only"
        risk_asymmetry = "asymmetric_downside"
        summary = (
            "severe geopolitical regime: escalation-sensitive, two-sided with asymmetric downside shock risk; "
            "continuation is tactical-only until there is a clean resolution path."
        )
    else:
        strictness = _text(dominant_profile.get("strictness")) or "normal"
        continuation_mode = _text(dominant_profile.get("continuation_mode")) or "normal"
        risk_asymmetry = _text(dominant_profile.get("risk_asymmetry")) or "neutral"
        summary = _text(dominant_profile.get("regime_summary"))

    return {
        "driver": "geopolitics",
        "severity": severity or _text(dominant_profile.get("regime_severity")) or "low",
        "flags": flags[:6],
        "dominant_event": _text(dominant.get("event")),
        "summary": summary,
        "risk_asymmetry": risk_asymmetry,
        "continuation_mode": continuation_mode,
        "strictness": strictness,
    }


def _parse_news_lines(raw_news) -> list[dict]:
    if isinstance(raw_news, str):
        lines = raw_news.splitlines()
    elif isinstance(raw_news, list):
        lines = []
        for item in raw_news:
            if isinstance(item, dict):
                line = _text(item.get("title")) or _text(item.get("text")) or _text(item.get("summary"))
            else:
                line = _text(item)
            if line:
                lines.append(line)
    else:
        lines = []

    out: list[dict] = []
    pattern = re.compile(
        r"^\s*-\s*(?:\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*МСК\]\s*)?"
        r"(?:\[(?P<impact>impact:[^\]]+)\]\s*)?(?P<title>.+?)\s*$",
        flags=re.IGNORECASE,
    )
    for raw_line in lines:
        line = _text(raw_line)
        if not line:
            continue
        match = pattern.match(line)
        if match:
            title = _text(match.group("title"))
            title = re.sub(r"\s+—\s+https?://\S+.*$", "", title)
            out.append(
                {
                    "timestamp_msk": _text(match.group("ts")),
                    "title": title,
                    "raw": line,
                }
            )
            continue
        cleaned = re.sub(r"\s+—\s+https?://\S+.*$", "", line)
        out.append({"timestamp_msk": "", "title": cleaned, "raw": line})
    return out


def _normalize_event_risk_item(item) -> dict | None:
    if isinstance(item, str):
        event = _event_label(item)
        if not event:
            return None
        return {
            "event": event,
            "source_title": "",
            "category": "",
            "phase": "",
            "impact": "",
            "directional_risk": "",
            "time_text": "",
            "drivers": [],
            "summary": "",
            "confirmed_facts": [],
            "anticipated_consequences": [],
            "realized_market_events": [],
            "recent_developments": [],
            "cluster_key": "",
            "cluster_actors": [],
            "cluster_locations": [],
            "cluster_themes": [],
            "regime_flags": [],
            "regime_severity": "",
            "regime_summary": "",
            "risk_asymmetry": "",
            "continuation_mode": "",
            "strictness": "",
            "headline_risk_delta": "NONE",
            "previous_risk_level": "",
            "new_risk_level": "",
            "risk_transition_reason": "",
            "duplicate_decision": "duplicate_headline_same_state",
            "headline_update_generated": False,
        }

    if not isinstance(item, dict):
        return None

    out = {
        "event": _event_label(item.get("event")),
        "source_title": _event_label(item.get("source_title")),
        "category": _normalize_category(item.get("category")),
        "phase": _normalize_phase(item.get("phase")),
        "impact": _normalize_impact(item.get("impact")),
        "directional_risk": _normalize_directional_risk(item.get("directional_risk")),
        "time_text": _text(item.get("time_text")),
        "drivers": _merge_unique_drivers(item.get("drivers") if isinstance(item.get("drivers"), list) else []),
        "summary": _merge_unique_texts(item.get("summary"), max_items=2),
        "confirmed_facts": _merge_semantic_text_list(item.get("confirmed_facts") if isinstance(item.get("confirmed_facts"), list) else []),
        "anticipated_consequences": _merge_semantic_text_list(
            item.get("anticipated_consequences") if isinstance(item.get("anticipated_consequences"), list) else []
        ),
        "realized_market_events": _merge_semantic_text_list(
            item.get("realized_market_events") if isinstance(item.get("realized_market_events"), list) else []
        ),
        "recent_developments": _merge_semantic_text_list(
            item.get("recent_developments") if isinstance(item.get("recent_developments"), list) else []
        ),
        "cluster_key": _text(item.get("cluster_key")),
        "cluster_actors": _unique_text_tokens(item.get("cluster_actors") if isinstance(item.get("cluster_actors"), list) else []),
        "cluster_locations": _unique_text_tokens(
            item.get("cluster_locations") if isinstance(item.get("cluster_locations"), list) else []
        ),
        "cluster_themes": _unique_text_tokens(item.get("cluster_themes") if isinstance(item.get("cluster_themes"), list) else []),
        "regime_flags": _unique_text_tokens(item.get("regime_flags") if isinstance(item.get("regime_flags"), list) else []),
        "regime_severity": _stronger_regime_severity(item.get("regime_severity")),
        "regime_summary": _merge_unique_texts(item.get("regime_summary"), max_items=2),
        "risk_asymmetry": _text(item.get("risk_asymmetry")),
        "continuation_mode": _text(item.get("continuation_mode")),
        "strictness": _text(item.get("strictness")),
        "headline_risk_delta": _text(item.get("headline_risk_delta")) if _text(item.get("headline_risk_delta")) in {"NONE", "MINOR_UPDATE", "ESCALATION", "DEESCALATION"} else "NONE",
        "previous_risk_level": _text(item.get("previous_risk_level")),
        "new_risk_level": _text(item.get("new_risk_level")),
        "risk_transition_reason": _text(item.get("risk_transition_reason")),
        "duplicate_decision": _text(item.get("duplicate_decision")) or "duplicate_headline_same_state",
        "headline_update_generated": bool(item.get("headline_update_generated")),
    }

    if not out["event"]:
        return None
    return out


def normalize_event_risk_snapshot(raw) -> dict:
    timestamp_utc = ""
    items_raw = []
    if isinstance(raw, dict):
        timestamp_utc = _text(raw.get("timestamp_utc"))
        items_raw = raw.get("event_risk_context") or []
    elif isinstance(raw, list):
        items_raw = raw

    out: list[dict] = []
    seen: dict[tuple[str, str, str], int] = {}
    for item in items_raw:
        normalized = _normalize_event_risk_item(item)
        if normalized is None:
            continue
        key = (
            normalized.get("category") or "",
            normalized.get("phase") or "",
            normalized.get("cluster_key") or _topic_identity(normalized.get("event")),
        )
        idx = seen.get(key)
        if idx is None:
            seen[key] = len(out)
            out.append(normalized)
            continue

        existing = out[idx]
        if len(normalized.get("event") or "") > len(existing.get("event") or ""):
            existing["event"] = normalized.get("event")
        if normalized.get("impact") == "high" and existing.get("impact") != "high":
            existing["impact"] = "high"
        elif normalized.get("impact") == "medium" and not existing.get("impact"):
            existing["impact"] = "medium"
        if normalized.get("directional_risk") and existing.get("directional_risk") in {"", "mixed", "uncertain"}:
            existing["directional_risk"] = normalized.get("directional_risk")
        if normalized.get("time_text") and not existing.get("time_text"):
            existing["time_text"] = normalized.get("time_text")
        existing["drivers"] = _merge_unique_drivers(existing.get("drivers"), normalized.get("drivers"))
        existing["summary"] = _merge_unique_texts(existing.get("summary"), normalized.get("summary"), max_items=2)
        existing["confirmed_facts"] = _merge_semantic_text_list(
            [*(existing.get("confirmed_facts") or []), *(normalized.get("confirmed_facts") or [])]
        )
        existing["anticipated_consequences"] = _merge_semantic_text_list(
            [*(existing.get("anticipated_consequences") or []), *(normalized.get("anticipated_consequences") or [])]
        )
        existing["realized_market_events"] = _merge_semantic_text_list(
            [*(existing.get("realized_market_events") or []), *(normalized.get("realized_market_events") or [])]
        )
        existing["recent_developments"] = _merge_semantic_text_list(
            [*(existing.get("recent_developments") or []), *(normalized.get("recent_developments") or [])]
        )
        existing["cluster_actors"] = _unique_text_tokens(
            [*(existing.get("cluster_actors") or []), *(normalized.get("cluster_actors") or [])]
        )
        existing["cluster_locations"] = _unique_text_tokens(
            [*(existing.get("cluster_locations") or []), *(normalized.get("cluster_locations") or [])]
        )
        existing["cluster_themes"] = _unique_text_tokens(
            [*(existing.get("cluster_themes") or []), *(normalized.get("cluster_themes") or [])]
        )
        existing["regime_flags"] = _unique_text_tokens([*(existing.get("regime_flags") or []), *(normalized.get("regime_flags") or [])])
        existing["regime_severity"] = _stronger_regime_severity(existing.get("regime_severity"), normalized.get("regime_severity"))
        existing["regime_summary"] = _merge_unique_texts(existing.get("regime_summary"), normalized.get("regime_summary"), max_items=2)
        if normalized.get("risk_asymmetry") and not existing.get("risk_asymmetry"):
            existing["risk_asymmetry"] = normalized.get("risk_asymmetry")
        if normalized.get("continuation_mode") and not existing.get("continuation_mode"):
            existing["continuation_mode"] = normalized.get("continuation_mode")
        if normalized.get("strictness") and not existing.get("strictness"):
            existing["strictness"] = normalized.get("strictness")
        if _headline_delta_rank(normalized.get("headline_risk_delta")) > _headline_delta_rank(existing.get("headline_risk_delta")):
            for key in (
                "headline_risk_delta",
                "previous_risk_level",
                "new_risk_level",
                "risk_transition_reason",
                "duplicate_decision",
                "headline_update_generated",
            ):
                existing[key] = normalized.get(key)

    out.sort(key=_event_risk_sort_key, reverse=True)

    critical_fields = _critical_topic_snapshot_fields(out)
    dominant_delta = _snapshot_headline_delta(out, critical_fields, _dominant_headline_delta(out))
    regime_layer = _build_event_risk_regime_layer(out)
    if critical_fields.get("headline_risk_active") and regime_layer:
        regime_layer = dict(regime_layer)
        regime_layer["severity"] = _stronger_regime_severity(
            regime_layer.get("severity"),
            critical_fields.get("event_risk_level"),
        ) or regime_layer.get("severity")
        if critical_fields.get("event_bias") == "risk_off":
            regime_layer["risk_asymmetry"] = "asymmetric_downside" if regime_layer.get("severity") == "severe" else "downside_elevated"
        regime_layer["strictness"] = "strict" if regime_layer.get("severity") == "severe" else "elevated"
        regime_layer["continuation_mode"] = "tactical_only" if regime_layer.get("severity") == "severe" else "confirmation_first"
        flags = regime_layer.get("flags") if isinstance(regime_layer.get("flags"), list) else []
        regime_layer["flags"] = _unique_text_tokens([*flags, "critical_topic_alarm", "headline_risk_active"])

    return {
        "timestamp_utc": timestamp_utc,
        "event_risk_context": out,
        "regime_layer": regime_layer,
        **critical_fields,
        **dominant_delta,
    }


def merge_event_risk_snapshots(*snapshots) -> dict:
    merged = {"timestamp_utc": "", "event_risk_context": []}
    for snapshot in snapshots:
        normalized = normalize_event_risk_snapshot(snapshot)
        if normalized.get("timestamp_utc"):
            merged["timestamp_utc"] = normalized.get("timestamp_utc")
        merged = normalize_event_risk_snapshot(
            {
                "timestamp_utc": merged.get("timestamp_utc"),
                "event_risk_context": [*merged.get("event_risk_context", []), *normalized.get("event_risk_context", [])],
            }
        )
    if merged.get("event_risk_context") and not merged.get("timestamp_utc"):
        merged["timestamp_utc"] = utc_now_iso()
    return merged


def _event_cluster_metadata(item: dict) -> dict:
    category = _text(item.get("category"))
    event = _text(item.get("event"))
    title = _text(item.get("source_title")) or event
    cluster_text = " ".join(
        [
            title,
            event,
            " ".join(item.get("confirmed_facts") or []),
            " ".join(item.get("anticipated_consequences") or []),
            " ".join(item.get("recent_developments") or []),
        ]
    )
    if category != "geopolitics":
        return {
            "cluster_key": f"{category}|{_topic_identity(event)}",
            "cluster_actors": [],
            "cluster_locations": [],
            "cluster_themes": [],
        }

    actors = _geopolitics_actor_tokens(cluster_text)
    locations = _geopolitics_location_tokens(cluster_text)
    themes = _geopolitics_theme_tokens(cluster_text)
    actor_key = "+".join(actors) or _topic_identity(event)
    region_key = _geopolitics_region_key(locations) or "general"
    low_cluster = cluster_text.lower()
    if region_key == "general" and "israel" in actors and ({"lebanon", "hezbollah", "hamas"} & set(actors)):
        region_key = "levant"
    if "ceasefire" in themes and any(
        marker in low_cluster
        for marker in (
            "ceasefire extended",
            "truce extended",
            "ceasefire extension",
            "truce extension",
            "extended by three weeks",
            "extended by 3 weeks",
            "ceasefire reached",
            "truce reached",
        )
    ) and not (set(themes) & {"blockade", "shipping", "military", "escalation", "sanctions", "tariffs"}):
        theme_key = "ceasefire_track"
    elif "talks" in themes and any(
        marker in low_cluster for marker in ("white house", "oval office", "press conference", "briefing", "remarks", "ambassador")
    ) and not (set(themes) & {"blockade", "shipping", "military", "sanctions", "tariffs"}):
        theme_key = "diplomatic_meeting"
    elif set(themes) & {"talks", "blockade", "shipping", "military", "escalation", "ceasefire", "sanctions", "tariffs"}:
        theme_key = "escalation_chain"
    else:
        theme_key = "+".join(themes[:2]) or _topic_identity(event)
    if theme_key == "escalation_chain":
        if region_key == "general" and actor_key in {"iran+us", "us+iran"}:
            region_key = "gulf"
        if region_key != "general":
            cluster_key = f"{category}|{region_key}|{theme_key}"
        else:
            cluster_key = f"{category}|{actor_key}|{theme_key}"
    else:
        cluster_key = f"{category}|{actor_key}|{region_key}|{theme_key}"
    return {
        "cluster_key": cluster_key,
        "cluster_actors": list(actors),
        "cluster_locations": list(locations),
        "cluster_themes": list(themes),
    }


def _cluster_event_risk_items(items: list[dict]) -> list[dict]:
    clusters: dict[str, dict] = {}
    order: list[str] = []

    for raw_item in items:
        item = dict(raw_item)
        item.update(_event_cluster_metadata(item))
        cluster_key = _text(item.get("cluster_key")) or _topic_identity(item.get("event"))
        if cluster_key not in clusters:
            clusters[cluster_key] = {
                **item,
                "confirmed_facts": list(item.get("confirmed_facts") or []),
                "anticipated_consequences": list(item.get("anticipated_consequences") or []),
                "realized_market_events": list(item.get("realized_market_events") or []),
                "recent_developments": list(item.get("recent_developments") or [item.get("event")]),
                "drivers": list(item.get("drivers") or []),
                "_source_titles": [item.get("source_title")] if item.get("source_title") else [],
            }
            order.append(cluster_key)
            continue

        existing = clusters[cluster_key]
        existing["confirmed_facts"] = _merge_semantic_text_list(
            [*(existing.get("confirmed_facts") or []), *(item.get("confirmed_facts") or [])]
        )
        existing["anticipated_consequences"] = _merge_semantic_text_list(
            [*(existing.get("anticipated_consequences") or []), *(item.get("anticipated_consequences") or [])]
        )
        existing["realized_market_events"] = _merge_semantic_text_list(
            [*(existing.get("realized_market_events") or []), *(item.get("realized_market_events") or [])]
        )
        existing["recent_developments"] = _merge_semantic_text_list(
            [*(existing.get("recent_developments") or []), *(item.get("recent_developments") or [item.get("event")])]
        )
        existing["drivers"] = _merge_unique_drivers(existing.get("drivers"), item.get("drivers"))
        existing["cluster_actors"] = _unique_text_tokens([*(existing.get("cluster_actors") or []), *(item.get("cluster_actors") or [])])
        existing["cluster_locations"] = _unique_text_tokens(
            [*(existing.get("cluster_locations") or []), *(item.get("cluster_locations") or [])]
        )
        existing["cluster_themes"] = _unique_text_tokens([*(existing.get("cluster_themes") or []), *(item.get("cluster_themes") or [])])
        existing["_source_titles"] = _unique_text_tokens([*(existing.get("_source_titles") or []), _text(item.get("source_title"))])
        existing["impact"] = _stronger_impact(existing.get("impact"), item.get("impact"))

    out: list[dict] = []
    for cluster_key in order:
        item = clusters[cluster_key]
        interpretation = {
            "confirmed_facts": _merge_semantic_text_list(item.get("confirmed_facts") or [], max_items=3),
            "anticipated_consequences": _merge_semantic_text_list(item.get("anticipated_consequences") or [], max_items=3),
            "realized_market_events": _merge_semantic_text_list(item.get("realized_market_events") or [], max_items=2),
            "recent_developments": _merge_semantic_text_list(item.get("recent_developments") or [], max_items=4),
        }
        interpretation["anticipated_consequences"] = _prune_anticipated_consequences(
            interpretation["confirmed_facts"],
            interpretation["anticipated_consequences"],
            interpretation["realized_market_events"],
        )
        interpretation["mixed_unresolved"] = bool(
            interpretation["confirmed_facts"] and interpretation["anticipated_consequences"] and not interpretation["realized_market_events"]
        )
        cluster_text = " ".join(
            [
                " ".join(item.get("_source_titles") or []),
                " ".join(interpretation.get("recent_developments") or []),
                " ".join(interpretation.get("confirmed_facts") or []),
                " ".join(interpretation.get("anticipated_consequences") or []),
                " ".join(interpretation.get("realized_market_events") or []),
            ]
        )
        phase = _classify_phase(cluster_text, category=item.get("category"), interpretation=interpretation)
        directional_risk = _classify_directional_risk(cluster_text, phase=phase, interpretation=interpretation)
        impact = _stronger_impact(item.get("impact"), _classify_impact(cluster_text, category=item.get("category"), phase=phase))
        interpretation["event"] = _event_label_from_interpretation(interpretation) or _event_label(item.get("event"))
        summary = _event_summary(
            cluster_text or item.get("event"),
            category=item.get("category"),
            phase=phase,
            directional_risk=directional_risk,
            interpretation=interpretation,
        )
        drivers = _event_drivers(
            cluster_text or item.get("event"),
            category=item.get("category"),
            phase=phase,
            directional_risk=directional_risk,
            interpretation=interpretation,
        )
        regime_profile = _geopolitical_regime_profile(
            {
                "event": interpretation.get("event"),
                "category": item.get("category"),
                "phase": phase,
                "impact": impact,
                "directional_risk": directional_risk,
                "summary": summary,
                "drivers": drivers,
                "confirmed_facts": interpretation.get("confirmed_facts") or [],
                "anticipated_consequences": interpretation.get("anticipated_consequences") or [],
                "realized_market_events": interpretation.get("realized_market_events") or [],
                "recent_developments": interpretation.get("recent_developments") or [],
                "cluster_themes": item.get("cluster_themes") or [],
                "source_title": " ".join(item.get("_source_titles") or []),
            }
        )
        headline_delta = _headline_risk_delta_for_cluster(
            item,
            interpretation,
            impact=impact,
            directional_risk=directional_risk,
        )
        out.append(
            {
                "event": interpretation.get("event"),
                "source_title": " ".join(item.get("_source_titles") or []),
                "category": item.get("category"),
                "phase": phase,
                "impact": impact,
                "directional_risk": directional_risk,
                "time_text": _time_text_for_phase(phase, interpretation),
                "confirmed_facts": interpretation.get("confirmed_facts") or [],
                "anticipated_consequences": interpretation.get("anticipated_consequences") or [],
                "realized_market_events": interpretation.get("realized_market_events") or [],
                "recent_developments": interpretation.get("recent_developments") or [],
                "drivers": drivers,
                "summary": summary,
                "cluster_key": cluster_key,
                "cluster_actors": item.get("cluster_actors") or [],
                "cluster_locations": item.get("cluster_locations") or [],
                "cluster_themes": item.get("cluster_themes") or [],
                "regime_flags": regime_profile.get("regime_flags") or [],
                "regime_severity": regime_profile.get("regime_severity") or "",
                "regime_summary": regime_profile.get("regime_summary") or "",
                "risk_asymmetry": regime_profile.get("risk_asymmetry") or "",
                "continuation_mode": regime_profile.get("continuation_mode") or "",
                "strictness": regime_profile.get("strictness") or "",
                **headline_delta,
            }
        )
    return out


def build_event_risk_context(raw_news, *, calendar_events=None) -> dict:
    items: list[dict] = []
    for line in _parse_news_lines(raw_news):
        title = _text(line.get("title"))
        if not title:
            continue
        if _is_scheduled_macro_title(title, calendar_events=calendar_events):
            continue
        category = _classify_category(title)
        if not category:
            continue
        interpretation = _interpret_event_title(title, category=category)
        phase = _classify_phase(title, category=category, interpretation=interpretation)
        directional_risk = _classify_directional_risk(title, phase=phase, interpretation=interpretation)
        impact = _classify_impact(title, category=category, phase=phase)
        items.append(
            {
                "event": interpretation.get("event") or _event_label(title),
                "category": category,
                "phase": phase,
                "impact": impact,
                "directional_risk": directional_risk,
                "time_text": _time_text_for_phase(phase, interpretation),
                "confirmed_facts": interpretation.get("confirmed_facts") or [],
                "anticipated_consequences": interpretation.get("anticipated_consequences") or [],
                "realized_market_events": interpretation.get("realized_market_events") or [],
                "recent_developments": interpretation.get("recent_developments") or [],
                "drivers": _event_drivers(
                    title,
                    category=category,
                    phase=phase,
                    directional_risk=directional_risk,
                    interpretation=interpretation,
                ),
                "summary": _event_summary(
                    title,
                    category=category,
                    phase=phase,
                    directional_risk=directional_risk,
                    interpretation=interpretation,
                ),
                "source_title": title,
            }
        )
    snapshot = normalize_event_risk_snapshot(
        {
            "timestamp_utc": utc_now_iso(),
            "event_risk_context": _cluster_event_risk_items(items),
        }
    )
    snapshot["event_risk_context"] = (snapshot.get("event_risk_context") or [])[:3]
    critical_fields = _critical_topic_snapshot_fields(snapshot.get("event_risk_context") or [])
    snapshot.update(critical_fields)
    return snapshot


def _profile_implication(item: dict, *, profile: str) -> str:
    source_title = _text(item.get("source_title")) or _text(item.get("event"))
    if _text(item.get("category")) == "crypto_market_structure" and _is_stablecoin_adoption_story(source_title):
        if profile == "day":
            return (
                "Риск исполнения: это фоновая liquidity-theme история, а не самостоятельный shock-driver; "
                "решение по сделке всё ещё должно идти от структуры и confirm."
            )
        return (
            "Режим: история поддерживает тему крипто-ликвидности, но сама по себе не задаёт новый стресс-режим."
        )

    phase = _text(item.get("phase"))
    directional_risk = _text(item.get("directional_risk"))
    confirmed_facts = item.get("confirmed_facts") or []
    anticipated_consequences = item.get("anticipated_consequences") or []
    mixed_unresolved = bool(confirmed_facts and anticipated_consequences and phase in {"pre_event", "ongoing"})
    regime_severity = _text(item.get("regime_severity")).lower()
    if _text(item.get("category")) == "geopolitics" and regime_severity == "severe":
        if profile == "day":
            return (
                "Риск исполнения: тяжёлый геополитический режим чувствителен к эскалации и несёт асимметричный downside-risk; "
                "continuation допустим только тактически, нужен ретест/подтверждение, invalidation должен быть явным."
            )
        return (
            "Режим: тяжёлый геополитический фон чувствителен к эскалации и несёт асимметричный downside-risk; "
            "continuation допустим только тактически, пока не появится чистый путь к развязке."
        )
    if profile == "day":
        if mixed_unresolved:
            return "Риск исполнения: один компонент уже подтверждён, но следующий шаг эскалации остаётся нерешённым; без погони за первыми заголовками."
        if phase in {"pre_event", "ongoing"}:
            return "Риск исполнения: чувствительность к заголовкам повышена; подтверждение важнее агрессивной погони."
        if directional_risk == "risk_on":
            return "Риск исполнения: relief-сценарий возможен, но перед continuation всё равно нужен confirm."
        return "Риск исполнения: волатильность может оставаться повышенной; лучше реагировать на confirm, а не на первый импульс."
    if mixed_unresolved:
        return "Режим: подтверждённое ухудшение делает фон хрупким, а нерешённая траектория эскалации всё ещё может изменить ближайшие 3-7 дней."
    if phase in {"pre_event", "ongoing"}:
        return "Режим: нерешённый катализатор может дестабилизировать risk-regime на горизонте 3-7 дней."
    if directional_risk == "risk_on":
        return "Режим: краткосрочный relief может стабилизировать фон, но follow-through всё ещё требует подтверждения."
    return "Режим: устойчивость risk-on слабее, пока катализатор сохраняет в рынке остаточную премию за риск."


def render_event_risk_context_section(snapshot, *, profile: str = "day") -> str:
    normalized = normalize_event_risk_snapshot(snapshot)
    items = normalized.get("event_risk_context") or []
    if not items:
        return ""

    profile_key = _text(profile).lower()
    title = "⚡ Катализаторы смены режима" if profile_key == "mid" else "⚡ Катализаторы событийного риска"
    max_items = 3 if profile_key == "mid" else 2
    lines = [title]
    regime_layer = normalized.get("regime_layer") if isinstance(normalized.get("regime_layer"), dict) else {}
    regime_summary = _text(regime_layer.get("summary"))
    if regime_summary:
        severity = _text(regime_layer.get("severity")) or "n/a"
        driver = _text(regime_layer.get("driver")) or "n/a"
        lines.append(f"- Слой режима [{severity} | {driver}]: {regime_summary}")
    for item in items[:max_items]:
        line = (
            f"- [{item.get('phase') or 'unknown'} | {item.get('impact') or 'n/a'} | "
            f"{item.get('category') or 'n/a'} | {item.get('directional_risk') or 'n/a'}] "
            f"{item.get('event')} ({item.get('time_text') or 'recent'}) — {item.get('summary')}"
        )
        lines.append(line)
        implication = _profile_implication(item, profile=profile_key)
        if implication:
            lines.append(f"  {implication}")
    return "\n".join(lines)


def attach_event_risk_context(
    payload: dict | None,
    snapshot,
    *,
    field_name: str = "event_risk_context",
    timestamp_field: str = "event_risk_context_timestamp_utc",
) -> dict | None:
    if not isinstance(payload, dict):
        return payload
    normalized = normalize_event_risk_snapshot(snapshot)
    payload[field_name] = copy.deepcopy(normalized.get("event_risk_context") or [])
    payload[timestamp_field] = normalized.get("timestamp_utc") or utc_now_iso()
    if field_name == "event_risk_context":
        for key in (
            "critical_topics",
            "dominant_critical_topic",
            "headline_risk_active",
            "headline_risk_delta",
            "previous_risk_level",
            "new_risk_level",
            "risk_transition_reason",
            "duplicate_decision",
            "headline_update_generated",
            "event_risk_level",
            "event_bias",
            "confirm_policy",
            "matched_entities",
            "matched_phrases",
            "escalation_reason",
        ):
            payload[key] = copy.deepcopy(normalized.get(key))
    return payload
