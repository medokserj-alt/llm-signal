from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, List

Direction = Literal["long", "short"]

class EntryRange(BaseModel):
    min: float
    max: float

class MultiTFView(BaseModel):
    m5: str
    m15: str
    h1: str
    h4: str
    d1: str


class UpcomingEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    time_msk: str | None = None
    date_msk: str | None = None
    event: str | None = None
    category: str | None = None
    impact: str | None = None
    window_before_min: float | None = None
    window_after_min: float | None = None
    expected_regime_effect: str | None = None
    note: str | None = None

class SignalPayload(BaseModel):
    time_msk: str
    symbol: str
    price: float
    direction: Direction
    entry_range: EntryRange
    sl: float
    tp1: float
    tp2: float
    rr: float
    take_profit_rules: str
    break_even_rule: str
    multi_tf_view: MultiTFView
    why_asset: str
    news_context: List[str] = []
    upcoming_events: List[UpcomingEvent] = Field(default_factory=list)
    macro_risk_summary: str = ""
    market_context: str
    validity_minutes: int
    cancel_condition: str
    technical_rationale: str
    disclaimer: str = "Не является инвестсоветом. NFA/DYOR. Управляйте риском."
