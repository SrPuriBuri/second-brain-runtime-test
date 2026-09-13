def qualifies(f, threshold):
    return (
        f.intraday_return > 0
        and f.relative_return >= threshold
        and f.rising
        and 0 <= 1 - f.close / f.high <= 0.005
    )
