"""Research persistence is append-only and limited to its own evidence subtree."""

import json
import re

from trading_runtime.config import SafetyError, redact
from trading_runtime.private_store import ROOT


def safety(store):
    readiness = store.required("state/readiness.json").json()
    if (
        store.read_strategy().tradable is not False
        or store.read_kill_switch().enabled is not True
        or readiness.get("execution_ready") is not False
        or readiness.get("execution_enabled") is not False
    ):
        raise SafetyError("RESEARCH_SAFETY_INVALID")
    return {
        "strategy_tradable": False,
        "kill_switch_enabled": True,
        "execution_enabled": False,
        "execution_ready": False,
    }


def persist(store, name, report):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SafetyError("RESEARCH_PATH_FORBIDDEN")
    safety(store)
    clean = json.loads(redact(json.dumps(report, allow_nan=False)))
    path = ROOT + "data/evidence/research-v1/" + name + ".json"
    store.create_append_only_record(path, clean)
    safety(store)
    return path
