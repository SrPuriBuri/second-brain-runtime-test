"""Manual research coordinator, with durable OOS claims and private summaries."""

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path

from trading_runtime.config import SafetyError
from trading_runtime.private_store import ROOT
from .bars import Dataset
from .data import digest
from .experiment import select, run_oos, save_json
from .reports import safety, persist
from .splits import validate_protocol


def run_cloud(source, store, cache):
    before = safety(store)
    pfile = store.required("research/protocol-v1.json")
    protocol_document = store.required("research/RESEARCH_PROTOCOL_V1.md")
    protocol = pfile.json()
    phash = validate_protocol(protocol)
    if store.read_file(ROOT + "data/evidence/research-v1/oos_" + phash + ".json"):
        raise SafetyError("RESEARCH_PROTOCOL_OOS_ALREADY_CONSUMED")
    policy = store.read_policy()
    start, end = protocol["warmup_start"], protocol["oos"][1]
    calendar = source.get("calendar", {"start": start, "end": end})
    bars = source.bars(
        protocol["symbols"], start, str(date.fromisoformat(end) + timedelta(days=1))
    )
    bundle = {
        "source": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "asof": "-",
        "timeframe": "5Min",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "calendar": calendar,
        "bars": bars,
    }
    data = Dataset(bundle)
    save_json(Path(cache) / "dataset.json", bundle)
    result = select(data, protocol, policy)
    name = os.environ.get(
        "GITHUB_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    )
    # Shard derived metrics to keep Contents API records inspectable and bounded.
    dev_refs = {}
    matrix = {}
    for variant, metrics in result.pop("development").items():
        key = "development_" + name + "_" + variant.replace(".", "p")
        dev_refs[variant] = persist(
            store,
            key,
            {
                "variant": variant,
                "protocol_hash": phash,
                "dataset_hash": data.hash,
                "metrics": metrics,
            },
        )
        matrix[variant] = {
            cost: {
                k: value
                for k, value in m.items()
                if not k.startswith(("by_", "excluding_"))
            }
            for cost, m in metrics.items()
        }
    result["development_refs"] = dev_refs
    result["development_matrix"] = matrix
    validation_refs = {}
    for variant, metrics in list(result["validation"].items()):
        validation_refs[variant] = persist(
            store, "validation_" + name + "_" + variant.replace(".", "p"), metrics
        )
        result["validation"][variant] = {
            "rejection_reasons": metrics["rejection_reasons"],
            "evidence_ref": validation_refs[variant],
        }
    for index, fold in enumerate(result["walkforward"]):
        if fold["test"]:
            ref = persist(store, "walkforward_" + name + "_" + str(index), fold)
            fold["test"] = {"evidence_ref": ref}
    result["validation_refs"] = validation_refs
    result["source_receipts"] = source.receipts
    result["protocol_blob_sha"] = pfile.sha
    result["protocol_document_blob_sha"] = protocol_document.sha
    result["request_count"] = source.requests
    result["safety_before"] = before
    result["safety_after"] = safety(store)
    # Freeze selected variant and all selection evidence before OOS can be evaluated.
    if (
        store.required("research/protocol-v1.json").sha != pfile.sha
        or store.required("research/RESEARCH_PROTOCOL_V1.md").sha
        != protocol_document.sha
        or store.read_policy() != policy
    ):
        raise SafetyError("RESEARCH_AUTHORITY_CHANGED")
    selection_ref = persist(store, "selection_" + name, result)
    if result["selected_variant"]:
        oos = run_oos(
            data,
            protocol,
            policy,
            result,
            Path(cache) / "claims",
            remote_claim=lambda claim: persist(store, "oos_" + phash, claim),
        )
        oos_ref = persist(store, "oos_result_" + name, oos)
        decision = oos["decision"]
        oos_status = "CONSUMED"
    else:
        oos_ref, decision, oos_status = None, "NO_GO", "SEALED"
    if (
        store.required("research/protocol-v1.json").sha != pfile.sha
        or store.read_policy() != policy
    ):
        raise SafetyError("RESEARCH_AUTHORITY_CHANGED")
    final = {
        "decision": decision,
        "oos_status": oos_status,
        "selected_variant": result["selected_variant"],
        "selection_ref": selection_ref,
        "oos_ref": oos_ref,
        "protocol_hash": digest(protocol),
        "dataset_hash": data.hash,
        "git_commit": os.getenv("GITHUB_SHA"),
        "safety": safety(store),
        "orders_submitted": 0,
        "orders_cancelled": 0,
        "positions_closed": 0,
        "dataset_archive": "EPHEMERAL_CACHE_ONLY_HASHED_PROVENANCE",
        "request_count": source.requests,
    }
    persist(store, "results_" + name, final)
    return final
