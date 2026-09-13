"""Strict schemas shared with the private canonical project."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class TradeProposal(StrictModel):
    proposal_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    strategy_version: int = Field(ge=0)
    trade_date: date
    generated_at: AwareDatetime
    symbol: str = Field(pattern=r"^[A-Z]{1,5}(\.[A-Z])?$")
    side: Literal["BUY"]
    thesis: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    entry_type: Literal["LIMIT"]
    entry_price_or_rule: float = Field(gt=0, le=100000)
    stop_price: float = Field(gt=0, le=100000)
    target_price: float = Field(gt=0, le=100000)
    max_entry_price: float = Field(gt=0, le=100000)
    expires_at: AwareDatetime
    confidence: float = Field(ge=0, le=1)
    risk_notes: list[str] = Field(max_length=20)
    data_timestamp: AwareDatetime
    data_feed: Literal["iex", "sip"]


class Strategy(StrictModel):
    status: Literal["RESEARCH_REQUIRED", "FROZEN"]
    version: int = Field(ge=0)
    tradable: StrictBool
    # A separately reviewed implementation must support this identifier.
    implementation: str | None = None


class Policy(StrictModel):
    mode: Literal["PAPER_ONLY"] = "PAPER_ONLY"
    max_new_positions: int = Field(default=3, ge=1, le=3)
    max_positions: int = Field(default=2, ge=1, le=2)
    virtual_risk_equity: float = Field(default=10000, gt=0)
    max_risk_fraction: float = Field(default=0.005, gt=0, le=0.005)
    daily_risk_fraction: float = Field(default=0.015, gt=0, le=0.015)
    min_reward_risk: float = Field(default=1.5, ge=1.5)
    max_position_notional: float = Field(default=2500, gt=0)
    strategy_max_quantity: int = Field(default=1000, gt=0)
    max_data_age_seconds: int = Field(default=120, gt=0, le=120)
    max_price: float = Field(default=1000, gt=5)
    min_price: float = Field(default=5, ge=5)
    min_daily_dollar_volume: float = Field(default=10000000, gt=0)
    max_spread_fraction: float = Field(default=0.005, gt=0, le=0.01)
    watchlist_limit: int = Field(default=10, ge=5, le=15)
    seeds: list[str] = Field(
        default_factory=lambda: [
            "SPY",
            "QQQ",
            "AAPL",
            "MSFT",
            "NVDA",
            "AMZN",
            "GOOGL",
            "META",
            "AMD",
            "JPM",
            "XOM",
            "WMT",
            "COST",
            "V",
            "UNH",
        ]
    )
    excluded_symbols: list[str] = Field(
        default_factory=lambda: [
            "TQQQ",
            "SQQQ",
            "SPXL",
            "SPXS",
            "SOXL",
            "SOXS",
            "UVXY",
            "SH",
            "PSQ",
        ]
    )


class KillSwitch(StrictModel):
    enabled: StrictBool
    reason: str
    updated_at: AwareDatetime | None
    updated_by: str


class RunRecord(StrictModel):
    run_id: str
    trade_date: date
    slot: str
    strategy_version: int
    timestamp: AwareDatetime
    status: Literal["CLAIMED", "COMPLETE", "FAILED"]
    evidence_refs: list[str] = []
    reason_codes: list[str] = []


class Handoff(StrictModel):
    run_id: str
    timestamp: AwareDatetime
    status: str
    next_action: str
    evidence_refs: list[str]
    unresolved: list[str]


class Watchlist(StrictModel):
    timestamp: AwareDatetime
    trade_date: date
    source: str
    data_feed: Literal["iex", "sip"]
    strategy_version: int
    candidates: list[dict] = Field(max_length=15)
    rejections: list[dict]
    notes: list[str]
