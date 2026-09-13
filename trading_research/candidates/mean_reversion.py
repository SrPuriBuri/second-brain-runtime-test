def qualifies(f, threshold):
    return f.close <= f.vwap * (1 - threshold) and f.rising
