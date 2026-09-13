def qualifies(f, threshold):
    return f.close > f.opening_high * (1 + threshold)
