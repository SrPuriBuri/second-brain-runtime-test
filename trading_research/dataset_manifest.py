"""Selection identity is independent of retrieval time; vintages bind content."""

from datetime import datetime, timezone
import hashlib
import json

from trading_runtime.config import SafetyError

REQUIRED = {
    "source",
    "feed",
    "symbols",
    "universe_version",
    "start",
    "end",
    "timeframe",
    "adjustment",
    "retrieval_parameters",
    "corporate_actions_version",
    "security_master_version",
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def selection_config(config):
    if set(config) != REQUIRED:
        raise SafetyError("DATASET_SELECTION_FIELDS")
    result = dict(config)
    result["symbols"] = sorted(set(s.upper() for s in config["symbols"]))
    if result["start"] > result["end"]:
        raise SafetyError("DATASET_INTERVAL_INVALID")
    return result


def dataset_id(config):
    return sha256({"schema_version": 1, "selection": selection_config(config)})


def manifest(config, payload, retention):
    selection = selection_config(config)
    identity, content = dataset_id(config), sha256(payload)
    return {
        "schema_version": 1,
        "dataset_id": identity,
        "snapshot_id": sha256({"dataset_id": identity, "content_sha256": content}),
        "selection": selection,
        "content_sha256": content,
        "state": "FROZEN",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "retention": retention,
        "raw_publication_allowed": False,
    }
