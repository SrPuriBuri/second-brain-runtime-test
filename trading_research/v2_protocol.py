"""Metadata-only V2 preregistration validation. No strategy/data execution API."""

from datetime import date
import hashlib
import json
from pathlib import Path

from trading_runtime.config import SafetyError
from .dataset_manifest import sha256
from .data_quality import pre_oos_interval

UNIVERSE = [
    "DIA",
    "IWM",
    "QQQ",
    "SPY",
    "XLB",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLU",
    "XLV",
    "XLY",
]
STAGES = ("development", "validation", "internal_holdout", "external_oos")
V1_FAMILIES = {
    "opening_momentum",
    "relative_strength",
    "gap_continuation",
    "mean_reversion",
}
MASK_RULES = {
    "all_13_inputs_required": True,
    "excluded_symbol_session": "NO_SIGNAL_QC_EXCLUDED",
    "invalid_feature_input": "NO_SIGNAL",
    "unexpected_missing_after_entry": "INDETERMINATE_BLOCKS_ACCEPTANCE",
    "indeterminate_allowed": 0,
    "interpolate": False,
    "forward_fill": False,
    "synthetic_ohlcv": False,
    "cross_day_features": False,
    "halt_clock": "PAUSE_TRADABLE_TIME_NEVER_WALL_FORCE_FLAT",
    "halt_overlap_feature_window": "NO_SIGNAL",
    "masked_future_known": "OFFLINE_QC_CENSORING_NOT_LIVE_ORACLE",
}


def protocol_hash(protocol):
    return sha256(protocol)


def load_protocol(path, *, expected_hash):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique
        )
        validate(value, expected_hash=expected_hash)
        return value
    except (OSError, ValueError, TypeError):
        raise SafetyError("V2_PROTOCOL_LOAD_INVALID") from None


def validate(protocol, *, expected_hash):
    """The expected digest must come from a separately pinned preregistration."""
    try:
        if protocol_hash(protocol) != expected_hash:
            raise ValueError("PROTOCOL_HASH")
        if protocol["schema_version"] != 2 or not {
            "simulation",
            "acceptance",
            "bootstrap",
            "controls",
            "metrics",
            "costs",
            "literature",
            "authority_file_hashes",
            "bindings",
            "sample_rationale",
        }.issubset(protocol):
            raise ValueError("PROTOCOL_SCHEMA")
        if set(protocol["authority_file_hashes"]) != {
            "STRATEGY.md",
            "POLICY.md",
            "state/kill-switch.json",
            "state/readiness.json",
        }:
            raise ValueError("AUTHORITY_BINDINGS")
        if protocol["phase"] != "3V2-C" or protocol["allowed_commands"] != ["validate"]:
            raise ValueError("PHASE_COMMANDS")
        scope = protocol["scope"]
        if scope != {
            "side": "LONG_ONLY",
            "asset_class": "UNLEVERAGED_US_EQUITY_ETF",
            "overnight": False,
            "news": False,
            "shorts": False,
            "options": False,
            "crypto": False,
            "strategy_execution_authorized": False,
        }:
            raise ValueError("SCOPE")
        if protocol["data"]["symbols"] != UNIVERSE:
            raise ValueError("UNIVERSE")
        if (
            protocol["data"]["feed"],
            protocol["data"]["timeframe"],
            protocol["data"]["adjustment"],
        ) != ("sip", "5Min", "raw"):
            raise ValueError("DATA_SELECTION")
        if protocol["mask_policy"] != MASK_RULES:
            raise ValueError("MASK_POLICY")
        splits = protocol["splits"]
        previous = None
        for name in STAGES:
            start, end = (date.fromisoformat(x) for x in splits[name])
            if start > end or (previous is not None and start <= previous):
                raise ValueError("SPLIT_CHRONOLOGY")
            previous = end
        if splits["internal_holdout"][1] >= "2025-01-01" or splits["external_oos"] != [
            "2025-01-01",
            "2026-08-31",
        ]:
            raise ValueError("OOS_BOUNDARY")
        if (
            splits["development"][0] < "2016-01-04"
            or splits["internal_holdout"][1] > "2024-12-31"
        ):
            raise ValueError("CHILD_COVERAGE")
        if (
            not protocol["oos_policy"]["separate_explicit_authorization_required"]
            or protocol["oos_policy"]["open_in_phase_c"]
        ):
            raise ValueError("OOS_SEALED")
        families = protocol["hypotheses"]
        if not 1 <= len(families) <= 3:
            raise ValueError("FAMILY_BUDGET")
        identifiers = []
        variants = []
        for family in families:
            if (
                family["id"] in V1_FAMILIES
                or family["mechanism"] != "CROSS_SECTIONAL_DISPERSION_CONVERGENCE"
            ):
                raise ValueError("V1_DUPLICATE_OR_UNSUPPORTED_MECHANISM")
            if (
                family["signal_schema"]
                != "SYNCHRONOUS_13_ETF_MEDIAN_MAD_WITH_RELATIVE_TURN"
            ):
                raise ValueError("V1_RENAME")
            if not family["v1_difference"] or not family["sources"]:
                raise ValueError("HYPOTHESIS_EVIDENCE")
            if not 1 <= len(family["variants"]) <= 3:
                raise ValueError("NEIGHBOR_BUDGET")
            if sum(v["role"] == "canonical" for v in family["variants"]) != 1:
                raise ValueError("CANONICAL_REQUIRED")
            identifiers.append(family["id"])
            variants.extend(v["id"] for v in family["variants"])
        budget = protocol["trial_budget"]
        if len(families) != 1 or len(variants) != 3:
            raise ValueError("DECLARED_SCHEDULE_REQUIRES_ONE_FAMILY_THREE_VARIANTS")
        declared = families[0]["variants"]
        if (
            sorted(v["lookback_minutes"] for v in declared) != [15, 30, 45]
            or next(v for v in declared if v["role"] == "canonical")["lookback_minutes"]
            != 30
            or any(v["role"] not in {"canonical", "neighbor"} for v in declared)
        ):
            raise ValueError("DECLARATIVE_NEIGHBOR_SCHEMA")
        if (
            len(variants) > 9
            or len(set(variants)) != len(variants)
            or len(set(identifiers)) != len(identifiers)
            or budget["primary_variants"] != len(variants)
            or budget["families"] != len(families)
            or budget["hidden_ablations_allowed"] is not False
        ):
            raise ValueError("TRIAL_BUDGET")
        counts = budget["record_counts"]
        if (
            counts
            != {
                "strategy_simulations": 22,
                "spy_control_simulations": 12,
                "no_trade_control_records": 4,
                "bootstrap_jobs": 6,
                "development_subperiod_diagnostics": 9,
            }
            or sum(counts.values()) != budget["max_evaluation_records"]
        ):
            raise ValueError("EVALUATION_BUDGET")
        if (
            protocol["selection"]["neighbor_promotion_allowed"]
            or protocol["selection"]["max_external_oos_candidates"] != 1
        ):
            raise ValueError("SELECTION")
        if protocol["safety"] != {
            "tradable": False,
            "kill_switch": True,
            "execution_enabled": False,
            "execution_ready": False,
        }:
            raise ValueError("SAFETY")
        return {
            "status": "PROTOCOL_VALID",
            "protocol_hash": expected_hash,
            "execution_available": False,
            "oos_access_available": False,
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SafetyError("V2_PROTOCOL_INVALID") from None


def verify_bindings(protocol, view, parent_manifest, checksums):
    """Validate metadata hashes without loading a single market-price row."""
    b = protocol["bindings"]
    try:
        actual = {
            "child_view_id": sha256(view),
            "parent_dataset_id": parent_manifest["dataset_id"],
            "parent_snapshot_id": parent_manifest["snapshot_id"],
            "universe_hash": sha256(view["mask"]["universe"]),
            "quality_mask_hash": sha256(view["mask"]),
            "halt_calendar_hash": sha256(view["mask"]["halts"]),
            "action_overlay_hash": sha256(view["actions"]),
            "parent_manifest_hash": sha256(parent_manifest),
            "parent_checksums_hash": sha256(checksums),
        }
        if (
            actual != b
            or view["parent_snapshot_id"] != b["parent_snapshot_id"]
            or view["parent_dataset_id"] != b["parent_dataset_id"]
            or view["parent_manifest_hash"] != b["parent_manifest_hash"]
            or view["parent_checksums_hash"] != b["parent_checksums_hash"]
            or view["mask"]["universe"] != UNIVERSE
            or sha256({k: v for k, v in parent_manifest.items() if k != "snapshot_id"})
            != b["parent_snapshot_id"]
            or parent_manifest["aggregate_content_hash"] != sha256(checksums)
            or parent_manifest["OOS_price_requests"] != 0
            or parent_manifest["strategy_return_calculations"] != 0
        ):
            raise ValueError()
        return "BINDINGS_PASS"
    except (KeyError, TypeError, ValueError):
        raise SafetyError("V2_BINDING_MISMATCH") from None


def verify_authorities(protocol, project):
    from trading_runtime.private_store import authority_json

    project = Path(project)
    for name, expected in protocol["authority_file_hashes"].items():
        path = (project / name).resolve()
        if (
            not path.is_relative_to(project.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise SafetyError("V2_AUTHORITY_CHANGED")
    strategy = authority_json((project / "STRATEGY.md").read_text(encoding="utf-8"))
    kill = json.loads((project / "state/kill-switch.json").read_text())
    ready = json.loads((project / "state/readiness.json").read_text())
    if (
        strategy["status"] != "RESEARCH_REQUIRED"
        or strategy["tradable"] is not False
        or kill["enabled"] is not True
        or ready["execution_enabled"] is not False
        or ready["execution_ready"] is not False
    ):
        raise SafetyError("V2_AUTHORITY_UNSAFE")
    return "AUTHORITIES_PASS"


def require_command(command):
    if command != "validate":
        raise SafetyError("V2_EXECUTION_UNAVAILABLE_PHASE_C")


def require_market_request(start, end):
    pre_oos_interval(start, end)
    raise SafetyError("V2_MARKET_ACCESS_UNAVAILABLE_PHASE_C")
