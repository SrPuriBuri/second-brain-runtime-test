"""Pure long-only setup functions. They receive immutable point-in-time features."""

from dataclasses import dataclass
from itertools import product

from .opening_momentum import qualifies as opening_momentum
from .relative_strength import qualifies as relative_strength
from .gap_continuation import qualifies as gap_continuation
from .mean_reversion import qualifies as mean_reversion

FAMILIES = {
    "opening_momentum": opening_momentum,
    "relative_strength": relative_strength,
    "gap_continuation": gap_continuation,
    "mean_reversion": mean_reversion,
}


@dataclass(frozen=True)
class Variant:
    family: str
    threshold: float
    regime: bool
    activity: bool

    @property
    def id(self):
        return f"{self.family}_{self.threshold:g}_r{int(self.regime)}_v{int(self.activity)}"


def grid(protocol):
    return tuple(
        Variant(family, threshold, regime, activity)
        for family in sorted(protocol["thresholds"])
        for threshold, regime, activity in product(
            protocol["thresholds"][family], (False, True), (False, True)
        )
    )


def signal(feature, variant, protocol, min_rr):
    if (
        not feature
        or (variant.regime and feature.regime != "favorable")
        or (variant.activity and feature.rvol < protocol["rvol_threshold"])
    ):
        return None
    if variant.family == "baseline":
        if feature.intraday_return <= 0:
            return None
    elif not FAMILIES[variant.family](feature, variant.threshold):
        return None
    reference = feature.close
    stop = feature.recent_low - reference * protocol["stop_buffer_fraction"]
    risk = reference - stop
    if (
        not protocol["min_stop_fraction"]
        <= risk / reference
        <= protocol["max_stop_fraction"]
    ):
        return None
    target = (
        feature.vwap
        if variant.family == "mean_reversion"
        else reference + protocol["target_r"] * risk
    )
    if (target - reference) / risk < min_rr:
        return None
    return {
        "side": "BUY",
        "reference": reference,
        "stop": stop,
        "target": target,
        "planned_risk": risk,
    }
