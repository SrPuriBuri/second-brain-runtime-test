"""Frozen PCG64 session-block resampling; independent of invocation order."""

import math
import numpy as np

from trading_runtime.config import SafetyError
from .provenance import Provenance, require_provenance, require_development_date


def block_indices(n, spec):
    if n < 5:
        raise SafetyError("D1_BOOTSTRAP_INSUFFICIENT_SESSIONS")
    b = spec.base["bootstrap"]
    rng = np.random.Generator(np.random.PCG64(b["seed"]))
    starts = rng.integers(0, n - 4, size=(b["resamples"], math.ceil(n / 5)), dtype=np.int64)
    indices = (starts[:, :, None] + np.arange(5, dtype=np.int64)).reshape(b["resamples"], -1)[:, :n]
    indices.setflags(write=False)
    return indices


def lower_bound(daily, stage, spec, *, source):
    mode = require_provenance(source, stage=stage)
    if mode is Provenance.DEVELOPMENT:
        for day in daily:
            require_development_date(day)
    days = sorted(daily)
    if any(type(daily[d]["count"]) is not int or daily[d]["count"] < 0 for d in days):
        raise SafetyError("D1_BOOTSTRAP_INVALID")
    totals = np.array([daily[d]["R"] for d in days], dtype=np.float64)
    counts = np.array([daily[d]["count"] for d in days], dtype=np.int64)
    if not np.isfinite(totals).all() or (counts < 0).any():
        raise SafetyError("D1_BOOTSTRAP_INVALID")
    indices = block_indices(len(days), spec)
    numerator = totals[indices].sum(axis=1)
    denominator = counts[indices].sum(axis=1)
    values = np.full(len(indices), -np.inf)
    np.divide(numerator, denominator, out=values, where=denominator > 0)
    values.sort()
    q = spec.base["bootstrap"]["lower_quantile"][stage]
    return float(values[math.ceil(len(values) * q) - 1])
