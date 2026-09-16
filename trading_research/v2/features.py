"""Same-session binary64 panel analytics from synthetic D1 inputs."""

from datetime import timedelta
import math

from trading_research.v2_protocol import UNIVERSE
from trading_runtime.config import SafetyError
from .clock import STEP, overlap_seconds, fully_halted
from .models import NY, require_synthetic


def median13(values):
    if len(values) != 13 or not all(math.isfinite(v) for v in values):
        raise SafetyError("D1_PANEL_INVALID")
    return sorted(values)[6]


def dispersion(moves, normalization):
    median = median13(list(moves.values()))
    mad = median13([abs(v - median) for v in moves.values()])
    if mad == 0 or not math.isfinite(mad):
        return None
    return median, mad, {s: (v - median) / (normalization * mad) for s, v in moves.items()}


def panel(session, spec, variant):
    require_synthetic(session)
    base = spec.base
    variants = {v["id"]: v for v in base["hypotheses"][0]["variants"]}
    if variant not in variants:
        raise SafetyError("D1_VARIANT")
    lookback = variants[variant]["lookback_minutes"]
    signal = base["simulation"]["signal"]
    hour, minute, second = map(int, signal["decision_time_ny"].split(":"))
    at = session.open.astimezone(NY).replace(hour=hour, minute=minute, second=second)
    begin = at - timedelta(minutes=65)
    if session.excluded.intersection(UNIVERSE):
        return None, "NO_SIGNAL_QC_EXCLUDED"
    if begin < session.open or at > session.close:
        return None, "NO_SIGNAL_WINDOW"
    if overlap_seconds(begin, at, session.halts):
        return None, "NO_SIGNAL_FEATURE_HALT"
    result, moves, turns = {}, {}, {}
    for symbol in UNIVERSE:
        required = [session.bar(symbol, begin + i * STEP) for i in range(13)]
        if any(b is None for b in required):
            return None, "NO_SIGNAL_MISSING_PANEL"
        visible = session.visible(symbol, at)
        ending = {b.end: b for b in required}
        last = ending[at]
        close = float(last.close_text)
        lag_close = float(ending[at - timedelta(minutes=lookback)].close_text)
        previous_close = float(ending[at - STEP].close_text)
        if not all(math.isfinite(x) and x > 0 for x in (close, lag_close, previous_close)):
            return None, "NO_SIGNAL_NONFINITE"
        if close / lag_close <= 0 or close / previous_close <= 0:
            return None, "NO_SIGNAL_NONFINITE"
        moves[symbol] = math.log(close / lag_close)
        turns[symbol] = math.log(close / previous_close)
        ranges = []
        for previous, current in zip(required, required[1:]):
            _, high, low, _, _ = map(float, current.values)
            prev = float(previous.close_text)
            ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
        atr = sum(ranges) / signal["parameters"]["atr_bars"]
        volume = sum(float(b.close_text) * float(b.volume_text) for b in visible
                     if not fully_halted(b.start, b.end, session.halts))
        if not all(math.isfinite(x) for x in (close, moves[symbol], turns[symbol], atr, volume)):
            return None, "NO_SIGNAL_NONFINITE"
        result[symbol] = {"close": close, "close_text": last.close_text, "atr": atr,
                          "dollar_volume": volume, "last_volume_text": last.volume_text}
    scaled = dispersion(moves, signal["parameters"]["mad_normalization"])
    if scaled is None:
        return None, "NO_SIGNAL_MAD_ZERO"
    median, mad, zs = scaled
    turn_median = median13(list(turns.values()))
    for symbol in UNIVERSE:
        result[symbol].update(move=moves[symbol], z=zs[symbol], relative_turn=turns[symbol] - turn_median)
    return {"at": at, "median": median, "mad": mad, "symbols": result}, None
