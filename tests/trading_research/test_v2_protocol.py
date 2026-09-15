"""Only synthetic metadata: never market rows or strategy execution."""

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from trading_research.dataset_manifest import sha256
from trading_research.v2_protocol import (
    MASK_RULES,
    UNIVERSE,
    load_protocol,
    protocol_hash,
    require_command,
    require_market_request,
    validate,
    verify_authorities,
    verify_bindings,
)
from trading_runtime.config import SafetyError


@pytest.fixture
def fixture_protocol():
    """Minimal schema fixture; does not contain a strategy implementation."""
    checksums = {}
    parent = {
        "dataset_id": "d" * 64,
        "aggregate_content_hash": sha256(checksums),
        "OOS_price_requests": 0,
        "strategy_return_calculations": 0,
    }
    parent["snapshot_id"] = sha256(parent)
    view = {
        "mask": {"universe": UNIVERSE, "halts": []},
        "actions": [],
        "parent_dataset_id": parent["dataset_id"],
        "parent_snapshot_id": parent["snapshot_id"],
        "parent_manifest_hash": sha256(parent),
        "parent_checksums_hash": sha256(checksums),
    }
    p = {
        "schema_version": 2,
        "phase": "3V2-C",
        "allowed_commands": ["validate"],
        "scope": {
            "side": "LONG_ONLY",
            "asset_class": "UNLEVERAGED_US_EQUITY_ETF",
            "overnight": False,
            "news": False,
            "shorts": False,
            "options": False,
            "crypto": False,
            "strategy_execution_authorized": False,
        },
        "data": {
            "symbols": list(UNIVERSE),
            "feed": "sip",
            "timeframe": "5Min",
            "adjustment": "raw",
        },
        "mask_policy": deepcopy(MASK_RULES),
        "splits": {
            "development": ["2016-01-04", "2021-12-31"],
            "validation": ["2022-01-01", "2023-12-31"],
            "internal_holdout": ["2024-01-01", "2024-12-31"],
            "external_oos": ["2025-01-01", "2026-08-31"],
        },
        "oos_policy": {
            "separate_explicit_authorization_required": True,
            "open_in_phase_c": False,
        },
        "hypotheses": [
            {
                "id": "synthetic_schema_fixture",
                "mechanism": "CROSS_SECTIONAL_DISPERSION_CONVERGENCE",
                "signal_schema": "SYNCHRONOUS_13_ETF_MEDIAN_MAD_WITH_RELATIVE_TURN",
                "v1_difference": "schema fixture only",
                "sources": ["synthetic"],
                "variants": [
                    {
                        "id": f"v{n}",
                        "lookback_minutes": n,
                        "role": "canonical" if n == 30 else "neighbor",
                    }
                    for n in [15, 30, 45]
                ],
            }
        ],
        "trial_budget": {
            "families": 1,
            "primary_variants": 3,
            "hidden_ablations_allowed": False,
            "record_counts": {
                "strategy_simulations": 22,
                "spy_control_simulations": 12,
                "no_trade_control_records": 4,
                "bootstrap_jobs": 6,
                "development_subperiod_diagnostics": 9,
            },
            "max_evaluation_records": 53,
        },
        "selection": {
            "neighbor_promotion_allowed": False,
            "max_external_oos_candidates": 1,
        },
        "safety": {
            "tradable": False,
            "kill_switch": True,
            "execution_enabled": False,
            "execution_ready": False,
        },
        "authority_file_hashes": {
            k: "fixture"
            for k in [
                "STRATEGY.md",
                "POLICY.md",
                "state/kill-switch.json",
                "state/readiness.json",
            ]
        },
        "bindings": {
            "child_view_id": sha256(view),
            "parent_dataset_id": parent["dataset_id"],
            "parent_snapshot_id": parent["snapshot_id"],
            "universe_hash": sha256(UNIVERSE),
            "quality_mask_hash": sha256(view["mask"]),
            "halt_calendar_hash": sha256([]),
            "action_overlay_hash": sha256([]),
            "parent_manifest_hash": sha256(parent),
            "parent_checksums_hash": sha256(checksums),
        },
    }
    for name in [
        "simulation",
        "acceptance",
        "bootstrap",
        "controls",
        "metrics",
        "costs",
        "literature",
        "sample_rationale",
    ]:
        p[name] = {"fixture_only": True}
    return p, view, parent, checksums


def test_deterministic_digest_and_pinned_load(fixture_protocol, tmp_path):
    p, *_ = fixture_protocol
    digest = protocol_hash(p)
    assert digest == protocol_hash(dict(reversed(list(p.items()))))
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(p, indent=4))
    assert load_protocol(path, expected_hash=digest) == p
    assert validate(p, expected_hash=digest)["execution_available"] is False
    with pytest.raises(SafetyError):
        load_protocol(path, expected_hash="0" * 64)
    path.write_text('{"x":1,"x":2}')
    with pytest.raises(SafetyError):
        load_protocol(path, expected_hash=sha256({"x": 2}))


@pytest.mark.parametrize(
    "binding",
    [
        "child_view_id",
        "parent_snapshot_id",
        "universe_hash",
        "quality_mask_hash",
        "halt_calendar_hash",
        "action_overlay_hash",
    ],
)
def test_every_bound_hash_is_required(fixture_protocol, binding):
    p, view, parent, sums = fixture_protocol
    assert verify_bindings(p, view, parent, sums) == "BINDINGS_PASS"
    p["bindings"][binding] = "0" * 64
    with pytest.raises(SafetyError):
        verify_bindings(p, view, parent, sums)


@pytest.mark.parametrize("stage", ["validation", "internal_holdout", "external_oos"])
def test_adjacent_splits_cannot_overlap(fixture_protocol, stage):
    p, *_ = fixture_protocol
    prior = {
        "validation": "development",
        "internal_holdout": "validation",
        "external_oos": "internal_holdout",
    }[stage]
    p["splits"][stage][0] = p["splits"][prior][1]
    with pytest.raises(SafetyError):
        validate(p, expected_hash=protocol_hash(p))


@pytest.mark.parametrize(
    "field",
    [
        "news",
        "shorts",
        "options",
        "crypto",
        "overnight",
        "strategy_execution_authorized",
    ],
)
def test_product_and_execution_scope_cannot_expand(fixture_protocol, field):
    p, *_ = fixture_protocol
    p["scope"][field] = True
    with pytest.raises(SafetyError):
        validate(p, expected_hash=protocol_hash(p))


@pytest.mark.parametrize(
    "change",
    [
        "families",
        "variants",
        "total",
        "xlre",
        "v1_renamed",
        "short_side",
        "oos",
        "commands",
        "indeterminate",
        "forward_fill",
        "neighbor",
    ],
)
def test_frozen_controls_reject_mutation(fixture_protocol, change):
    p, *_ = fixture_protocol
    if change == "families":
        p["hypotheses"] *= 4
    elif change == "variants":
        p["hypotheses"][0]["variants"] *= 4
    elif change == "total":
        p["trial_budget"]["max_evaluation_records"] = 999
    elif change == "xlre":
        p["data"]["symbols"].append("XLRE")
    elif change == "v1_renamed":
        p["hypotheses"][0]["signal_schema"] = "OWN_PRICE_BELOW_VWAP"
    elif change == "short_side":
        p["scope"]["side"] = "LONG_SHORT"
    elif change == "oos":
        p["oos_policy"]["open_in_phase_c"] = True
    elif change == "commands":
        p["allowed_commands"].append("backtest")
    elif change == "indeterminate":
        p["mask_policy"]["indeterminate_allowed"] = 1
    elif change == "forward_fill":
        p["mask_policy"]["forward_fill"] = True
    elif change == "neighbor":
        p["selection"]["neighbor_promotion_allowed"] = True
    with pytest.raises(SafetyError):
        validate(p, expected_hash=protocol_hash(p))


@pytest.mark.parametrize(
    "command", ["backtest", "development", "validation", "holdout", "oos", "trade"]
)
def test_execution_commands_unavailable(command):
    with pytest.raises(SafetyError):
        require_command(command)


@pytest.mark.parametrize(
    "start,end",
    [
        ("2025-01-01", "2025-02-01"),
        ("2024-12-31", "2025-01-01"),
        ("2020-01-01", "2020-01-02"),
    ],
)
def test_all_phase_c_market_access_forbidden(start, end):
    with pytest.raises(SafetyError):
        require_market_request(start, end)


def test_safety_authority_bytes_unchanged(fixture_protocol, tmp_path):
    p, *_ = fixture_protocol
    (tmp_path / "state").mkdir()
    (tmp_path / "STRATEGY.md").write_text(
        '```json\n{"status":"RESEARCH_REQUIRED","tradable":false}\n```'
    )
    (tmp_path / "POLICY.md").write_text("synthetic fixture")
    (tmp_path / "state/kill-switch.json").write_text('{"enabled":true}')
    (tmp_path / "state/readiness.json").write_text(
        '{"execution_enabled":false,"execution_ready":false}'
    )
    p["authority_file_hashes"] = {
        n: hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()
        for n in p["authority_file_hashes"]
    }
    assert verify_authorities(p, tmp_path) == "AUTHORITIES_PASS"
    (tmp_path / "POLICY.md").write_text("altered")
    with pytest.raises(SafetyError):
        verify_authorities(p, tmp_path)


def test_metadata_module_has_no_performance_or_broker_imports():
    import trading_research.v2_protocol as module

    tree = ast.parse(Path(module.__file__).read_text())
    forbidden = {
        "simulator",
        "candidates",
        "metrics",
        "experiment",
        "downloader",
        "providers",
        "executor",
        "alpaca",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not set((node.module or "").split(".")) & forbidden
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not set(alias.name.split(".")) & forbidden
    assert not any(
        hasattr(module, name)
        for name in ["simulate", "backtest", "submit_order", "run_strategy"]
    )
