"""Fixed-instrument experiment; never reinterpret today's screener as historical."""


def eligible(feature, protocol, policy):
    return (
        feature is not None
        and feature.symbol in protocol["symbols"]
        and feature.symbol in {"SPY", "QQQ"}
        and policy.min_price <= feature.close <= policy.max_price
        and feature.dollar_volume >= policy.min_daily_dollar_volume
    )
