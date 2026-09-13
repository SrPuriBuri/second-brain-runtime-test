from dataclasses import dataclass


@dataclass(frozen=True)
class Costs:
    half_spread_bps: float
    slippage_bps: float
    exit_fee_bps: float

    def __post_init__(self):
        import math

        if any(
            not math.isfinite(x) or x < 0
            for x in (self.half_spread_bps, self.slippage_bps, self.exit_fee_bps)
        ):
            raise ValueError("INVALID_COST")

    def entry(self, price):
        return price * (1 + (self.half_spread_bps + self.slippage_bps) / 10000)

    def exit(self, price):
        return price * (
            1 - (self.half_spread_bps + self.slippage_bps + self.exit_fee_bps) / 10000
        )
