"""Price-only gap hypothesis. This does not validate a news catalyst strategy."""


def qualifies(f, threshold):
    return f.gap >= threshold and f.intraday_return > 0
