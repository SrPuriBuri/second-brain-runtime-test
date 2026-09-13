"""Deterministic authority: callers cannot supply an already-approved decision."""

from dataclasses import dataclass
from decimal import Decimal

from .config import SafetyError
from .market_calendar import aware
from .risk import dec, size_position
from .slots import ENTRY_SLOTS, due_slots, slot_times


@dataclass(frozen=True)
class PolicyDecision:
    decision: str
    reason_codes: tuple[str, ...]
    quantity: int = 0


def evaluate(
    proposal,
    policy,
    strategy,
    kill_switch,
    snapshot,
    session,
    schedule,
    slot,
    now,
    clock,
    market_evidence,
    asset_allowed,
    duplicate=False,
    strategy_qualified=False,
):
    reasons = []

    def reject(condition, reason):
        if condition:
            reasons.append(reason)

    reject(kill_switch.enabled, "KILL_SWITCH")
    reject(
        not strategy.tradable or strategy.status != "FROZEN", "STRATEGY_NOT_TRADABLE"
    )
    reject(
        strategy.version != proposal.strategy_version or strategy.version == 0,
        "UNKNOWN_STRATEGY_VERSION",
    )
    reject(not strategy_qualified, "STRATEGY_RULES_NOT_VERIFIED")
    reject(not snapshot.reconciled, "UNRECONCILED_ACCOUNT")
    reject(
        not clock["is_open"]
        or session is None
        or not (session.open <= now < session.close),
        "MARKET_CLOSED",
    )
    reject(
        abs((now - aware(clock["timestamp"])).total_seconds())
        > policy.max_data_age_seconds,
        "STALE_BROKER_CLOCK",
    )
    reject(
        slot not in ENTRY_SLOTS or slot not in due_slots(now, session, schedule),
        "WRONG_SLOT",
    )
    reject(proposal.side != "BUY", "SIDE_NOT_ALLOWED")
    reject(not asset_allowed, "ASSET_NOT_TRADABLE")
    reject(duplicate, "DUPLICATE_ORDER")
    reject(
        proposal.trade_date.isoformat() != (session.trade_date if session else "")
        or proposal.expires_at <= now
        or proposal.generated_at > now,
        "EXPIRED_PROPOSAL",
    )
    if session:
        reject(
            proposal.expires_at > slot_times(session, schedule)["FORCE_FLAT"],
            "INVALID_EXPIRY",
        )
    age = (now - proposal.data_timestamp).total_seconds()
    reject(age < 0 or age > policy.max_data_age_seconds, "STALE_DATA")
    if market_evidence is None:
        reasons.append("STALE_DATA")
    else:
        age = (now - aware(market_evidence["timestamp"])).total_seconds()
        reject(
            age < 0
            or age > policy.max_data_age_seconds
            or proposal.data_feed != market_evidence["feed"],
            "STALE_DATA",
        )
        reject(
            market_evidence["ask"] > proposal.max_entry_price,
            "MAX_ENTRY_PRICE_EXCEEDED",
        )
    reject(snapshot.position_count >= policy.max_positions, "POSITION_LIMIT")
    reject(snapshot.new_positions >= policy.max_new_positions, "DAILY_TRADE_LIMIT")
    reject(
        any(p["symbol"] == proposal.symbol for p in snapshot.positions)
        or any(o["symbol"] == proposal.symbol for o in snapshot.open_orders),
        "EXISTING_POSITION",
    )
    reject(
        not policy.min_price <= proposal.entry_price_or_rule <= policy.max_price
        or proposal.entry_price_or_rule > proposal.max_entry_price,
        "INVALID_ENTRY",
    )
    reject(
        any(
            dec(v) != dec(v).quantize(Decimal("0.01"))
            for v in (
                proposal.entry_price_or_rule,
                proposal.stop_price,
                proposal.target_price,
            )
        ),
        "INVALID_PRICE_PRECISION",
    )
    size = None
    try:
        size = size_position(
            proposal, policy, snapshot.account["buying_power"], snapshot.account["cash"]
        )
        reject(
            size.reward_risk < dec(policy.min_reward_risk), "INSUFFICIENT_REWARD_RISK"
        )
        reject(
            size.risk > dec(policy.virtual_risk_equity) * dec(policy.max_risk_fraction),
            "RISK_TOO_HIGH",
        )
        reject(
            snapshot.realized_loss + snapshot.open_risk + size.risk
            > dec(policy.virtual_risk_equity) * dec(policy.daily_risk_fraction),
            "DAILY_LOSS_LIMIT",
        )
    except SafetyError as exc:
        reasons.append(str(exc))
    return PolicyDecision(
        "REJECT" if reasons else "PASS",
        tuple(dict.fromkeys(reasons)),
        size.quantity if size and not reasons else 0,
    )
