from __future__ import annotations

import copy
import re

from event_calendar import utc_now_iso

_VALID_CATEGORIES = {
    "geopolitics",
    "macro_policy_shock",
    "crypto_market_structure",
}
_VALID_PHASES = {"pre_event", "ongoing", "post_event"}
_VALID_IMPACTS = {"high", "medium", "low"}
_VALID_DIRECTIONAL_RISKS = {"risk_on", "risk_off", "uncertain", "mixed", "neutral"}

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
    "de-escalation",
    "sanctions relief",
    "reopened",
    "resume shipping",
    "approved",
    "approval",
    "backstop",
    "support package",
)

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
        ("red sea", "red_sea"),
        ("gaza", "gaza"),
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
    return any(marker in low for marker in ("talks", "peace talks", "negotiation", "negotiations", "meeting", "ceasefire"))


def _geopolitics_topic_prefix(low: str) -> str:
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
    if any(needle in low for needle in _CRYPTO_MARKET_STRUCTURE_NEEDLES) and any(
        needle in low for needle in _CRYPTO_SHOCK_NEEDLES
    ):
        return "crypto_market_structure"
    if any(needle in low for needle in _GEOPOLITICS_NEEDLES):
        return "geopolitics"
    if any(needle in low for needle in _MACRO_POLICY_NEEDLES):
        return "macro_policy_shock"
    if any(needle in low for needle in _CRYPTO_MARKET_STRUCTURE_NEEDLES):
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
    high_needles = (
        "hormuz",
        "blockade",
        "shipping",
        "strike",
        "missile",
        "attack",
        "sanctions",
        "tariff",
        "emergency",
        "liquidation",
        "outage",
        "hack",
        "exploit",
        "etf",
        "sec",
        "lawsuit",
    )
    if any(needle in low for needle in high_needles):
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
        return "the next escalation step is not yet confirmed"
    if " risk " in f" {text.lower()} ":
        if text.lower().endswith("risk increased"):
            return f"{text[:-len('risk increased')].strip()} is not yet confirmed".replace("  ", " ")
        if text.lower().endswith("risk remains elevated"):
            return f"{text[:-len('risk remains elevated')].strip()} is not yet confirmed".replace("  ", " ")
        if text.lower().endswith("risk remains"):
            return f"{text[:-len('risk remains')].strip()} is not yet confirmed".replace("  ", " ")
        if text.lower().endswith("risk stays"):
            return f"{text[:-len('risk stays')].strip()} is not yet confirmed".replace("  ", " ")
    if text.lower().endswith("are still ahead"):
        return text
    return f"{text} remains unresolved"


def _execution_tail_text(category: str, *, phase: str, mixed_unresolved: bool) -> str:
    if category == "geopolitics":
        if mixed_unresolved:
            return "Headline sensitivity and volatility remain elevated."
        if phase in {"pre_event", "ongoing"}:
            return "Headline sensitivity remains elevated before clarity."
        return "Risk premium stays elevated until follow-through is clearer."
    if category == "crypto_market_structure":
        return "Execution risk remains elevated until forced flows clear."
    return "Cross-asset volatility can stay elevated until follow-through is clearer."


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
            f"{label} is still ahead, and headline sensitivity remains elevated before the outcome is clear."
        )
    if phase == "ongoing":
        if confirmed_facts and any("ongoing" in fact.lower() or "resumed" in fact.lower() for fact in confirmed_facts):
            return "Negotiations are ongoing and the market is still waiting for clarity; price action remains headline-driven."
        return (
            f"{label} remains unresolved, so markets stay headline-sensitive and confirmation matters more "
            "than aggressive chasing."
        )
    if directional_risk == "risk_on":
        return (
            f"{label} supports short-term relief, but follow-through remains headline-sensitive until the "
            "move is confirmed."
        )
    if category == "crypto_market_structure":
        return (
            f"{label} raises immediate execution risk and can keep price action disorderly until forced flows "
            "clear out."
        )
    return (
        f"{label} keeps risk premium elevated and increases headline-driven volatility until the market gets "
        "clearer follow-through."
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
        }

    if not isinstance(item, dict):
        return None

    out = {
        "event": _event_label(item.get("event")),
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

    out.sort(key=_event_risk_sort_key, reverse=True)

    return {
        "timestamp_utc": timestamp_utc,
        "event_risk_context": out,
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
    if set(themes) & {"talks", "blockade", "shipping", "military", "escalation", "ceasefire", "sanctions", "tariffs"}:
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
        out.append(
            {
                "event": interpretation.get("event"),
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
    return snapshot


def _profile_implication(item: dict, *, profile: str) -> str:
    phase = _text(item.get("phase"))
    directional_risk = _text(item.get("directional_risk"))
    confirmed_facts = item.get("confirmed_facts") or []
    anticipated_consequences = item.get("anticipated_consequences") or []
    mixed_unresolved = bool(confirmed_facts and anticipated_consequences and phase in {"pre_event", "ongoing"})
    if profile == "day":
        if mixed_unresolved:
            return "Execution: one component is confirmed, but the next escalation step is unresolved; avoid chasing first headlines."
        if phase in {"pre_event", "ongoing"}:
            return "Execution: headline sensitivity is elevated; prefer confirmation over aggressive chasing."
        if directional_risk == "risk_on":
            return "Execution: relief is possible, but confirmation still matters before chasing continuation."
        return "Execution: volatility can stay elevated; react to confirmation instead of first-move momentum."
    if mixed_unresolved:
        return "Regime: confirmed deterioration keeps the backdrop fragile while the unresolved escalation path can still reshape the next 3-7 days."
    if phase in {"pre_event", "ongoing"}:
        return "Regime: unresolved catalyst can destabilize the next 3–7 day risk regime."
    if directional_risk == "risk_on":
        return "Regime: short-term relief can stabilize the backdrop, but follow-through still needs confirmation."
    return "Regime: risk-on stability is weaker while the catalyst keeps a residual risk premium in the tape."


def render_event_risk_context_section(snapshot, *, profile: str = "day") -> str:
    normalized = normalize_event_risk_snapshot(snapshot)
    items = normalized.get("event_risk_context") or []
    if not items:
        return ""

    profile_key = _text(profile).lower()
    title = "⚡ Regime-changing catalysts" if profile_key == "mid" else "⚡ Event-risk catalysts"
    max_items = 3 if profile_key == "mid" else 2
    lines = [title]
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
    return payload
