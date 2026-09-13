"""Conservative metadata filters; bounded seeds prevent an unbounded AI input."""

import re


def asset_reason(asset, policy):
    if (
        asset.get("class") != "us_equity"
        or asset.get("status") != "active"
        or not asset.get("tradable")
    ):
        return "ASSET_NOT_TRADABLE"
    if asset.get("exchange") not in {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}:
        return "EXCHANGE_EXCLUDED"
    symbol, name = asset.get("symbol", ""), asset.get("name", "")
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", symbol) or re.search(
        r"\btest\b|when issued|warrant|right[s]?\b", name, re.I
    ):
        return "INVALID_ASSET"
    if symbol in policy.excluded_symbols or re.search(
        r"\b(?:inverse|leveraged|ultra|ultrapro|ultrashort|bear|short|[2-9]x)\b",
        name,
        re.I,
    ):
        return "LEVERAGED_OR_INVERSE"
    return None


def bounded_universe(assets, policy):
    by_symbol = {a["symbol"]: a for a in assets}
    accepted, rejected = [], []
    for symbol in policy.seeds[:50]:
        asset = by_symbol.get(symbol)
        reason = asset_reason(asset, policy) if asset else "ASSET_NOT_FOUND"
        if reason:
            rejected.append({"symbol": symbol, "reason": reason})
        else:
            accepted.append(symbol)
    return accepted, rejected
