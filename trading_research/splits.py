"""Chronological samples with no parameter or performance access to sealed OOS."""

from pathlib import Path
import json
from .data import digest
from trading_runtime.config import SafetyError


def validate_protocol(p):
    dev, val, oos = p["development"], p["validation"], p["oos"]
    if not (p["warmup_start"] < dev[0] <= dev[1] < val[0] <= val[1] < oos[0] <= oos[1]):
        raise SafetyError("OVERLAPPING_RESEARCH_SPLITS")
    if p["symbols"] != ["SPY", "QQQ"] or p["feed"] != "iex" or p["adjustment"] != "raw":
        raise SafetyError("UNSUPPORTED_RESEARCH_PROTOCOL")
    return digest(p)


def sample_days(dataset, protocol, sample, oos_authorized=False):
    validate_protocol(protocol)
    if sample == "oos" and not oos_authorized:
        raise SafetyError("OOS_SEALED")
    if sample not in {"development", "validation", "oos"}:
        raise SafetyError("UNKNOWN_RESEARCH_SAMPLE")
    start, end = protocol[sample]
    return [d for d in dataset.days if start <= d <= end]


def claim_local_oos(directory, selection, protocol, dataset_hash):
    if (
        selection["protocol_hash"] != digest(protocol)
        or selection["dataset_hash"] != dataset_hash
        or not selection.get("selected_variant")
    ):
        raise SafetyError("OOS_SELECTION_MISMATCH")
    path = Path(directory) / ("oos-" + digest(protocol) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(selection, handle, sort_keys=True)
    except FileExistsError:
        raise SafetyError("OOS_ALREADY_CONSUMED") from None
    return path
