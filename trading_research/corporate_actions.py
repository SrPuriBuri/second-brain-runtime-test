"""Type-aware event normalization; processing date is never announcement time."""

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import aware


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    action_type: str
    source: str
    retrieved_at: str
    source_record_id: str
    announcement_date: str | None = None
    effective_date: str | None = None
    ex_date: str | None = None
    record_date: str | None = None
    payment_date: str | None = None
    process_date: str | None = None
    old_symbol: str | None = None
    new_symbol: str | None = None
    known_at: str | None = None
    terms: dict = field(default_factory=dict)

    def __post_init__(self):
        aware(self.retrieved_at)
        for key in (
            "announcement_date",
            "effective_date",
            "ex_date",
            "record_date",
            "payment_date",
            "process_date",
        ):
            if getattr(self, key):
                date.fromisoformat(getattr(self, key))
        if self.known_at:
            aware(self.known_at)
        if not self.symbol or not self.source_record_id:
            raise SafetyError("CORPORATE_ACTION_ID_REQUIRED")


def normalize_alpaca(kind, row, retrieved_at):
    symbol = (
        row.get("symbol")
        or row.get("old_symbol")
        or row.get("source_symbol")
        or row.get("acquiree_symbol")
    )
    terms = {
        k: row[k]
        for k in (
            "new_rate",
            "old_rate",
            "rate",
            "cash_rate",
            "source_rate",
            "acquirer_rate",
            "acquiree_rate",
            "alternate_rate",
            "special",
            "foreign",
            "currency",
            "acquirer_symbol",
            "acquiree_symbol",
            "source_symbol",
            "alternate_symbol",
        )
        if k in row
    }
    if kind in {"forward_splits", "reverse_splits"}:
        try:
            old, new = Decimal(str(row["old_rate"])), Decimal(str(row["new_rate"]))
            if not old.is_finite() or not new.is_finite() or old <= 0 or new <= 0:
                raise ValueError()
            terms["share_multiplier"] = str(new / old)
            terms["historical_price_multiplier"] = str(old / new)
        except (KeyError, ValueError, InvalidOperation):
            raise SafetyError("INVALID_SPLIT_RATIO") from None
    result = CorporateAction(
        symbol=symbol,
        action_type=kind,
        source="alpaca",
        retrieved_at=retrieved_at,
        source_record_id=str(row.get("id", "")),
        announcement_date=row.get("declaration_date"),
        effective_date=row.get("effective_date"),
        ex_date=row.get("ex_date"),
        record_date=row.get("record_date"),
        payment_date=row.get("payable_date"),
        process_date=row.get("process_date"),
        old_symbol=row.get("old_symbol"),
        new_symbol=row.get("new_symbol"),
        terms=terms,
    )
    return asdict(result)
