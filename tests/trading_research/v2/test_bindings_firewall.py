import ast
import hashlib
from pathlib import Path
import shutil

import pytest

from trading_research.v2.bindings import PINS, Spec, canonical, digest, read_json, require_pins
from trading_research.v2.runner import require_command, main, metadata_path, validate_metadata
from trading_research.v2.models import Bar, SyntheticSession
from trading_research.v2_protocol import require_command as phase_c_command
from trading_runtime.config import SafetyError
from .conftest import SOURCE, at


@pytest.mark.parametrize("key", list(PINS))
def test_invariant_independent_pins(key):
    candidate = {**PINS, key: "0"*64}
    with pytest.raises(SafetyError, match="BINDING"):
        require_pins(candidate)


def test_golden_effective_hash(spec):
    assert digest({k:v for k,v in PINS.items() if k != "latest_effective_execution_spec_hash"}) == PINS["latest_effective_execution_spec_hash"]
    assert spec.config()["data_mode"] == "SYNTHETIC_ONLY"
    assert spec.clarification(4)["clarification_004_id"] == "D1-SEVERE-TAILS-004"


@pytest.mark.parametrize("filename", ["protocol-v2.json"] + [f"PROTOCOL_CLARIFICATION_D1_{n:03}.json" for n in range(1,5)])
def test_invariant_authority_tamper(spec_dir, tmp_path, filename):
    for p in spec_dir.glob("*.json"):
        if p.name == "protocol-v2.json" or (p.name.startswith("PROTOCOL_CLARIFICATION_D1_") and "MANIFEST" not in p.name):
            shutil.copyfile(p, tmp_path / p.name)
    value = read_json(tmp_path / filename)
    value["unauthorized_semantic_change"] = True
    (tmp_path / filename).write_bytes(canonical(value))
    with pytest.raises(SafetyError):
        Spec(tmp_path)


@pytest.mark.parametrize("name", ["STRATEGY.md","POLICY.md","state/kill-switch.json","state/readiness.json"])
def test_invariant_safety_authority_tamper(spec, spec_dir, tmp_path, name):
    from trading_research.v2_protocol import verify_authorities
    project = spec_dir.parents[1]
    for file in spec.base["authority_file_hashes"]:
        target = tmp_path / file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / file, target)
    target = tmp_path / name
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(SafetyError, match="AUTHORITY"):
        verify_authorities(spec.base, tmp_path)


@pytest.mark.parametrize("command", ["development","validation","holdout","oos","paper","live","trade"])
def test_invariant_forbidden_command(command):
    with pytest.raises(SafetyError, match="COMMAND"):
        require_command(command)
    with pytest.raises(SystemExit):
        main([command])


def test_invariant_phase_c_not_weakened():
    with pytest.raises(SafetyError):
        phase_c_command("conformance")


def test_invariant_future_prices_forbidden():
    future = at("13:00").replace(year=2025)
    with pytest.raises(SafetyError):
        Bar(future, "100","100","100","100","100")
    with pytest.raises(SafetyError):
        SyntheticSession(source=SOURCE, opened=future, closed=future.replace(hour=16), bars={})


def test_invariant_no_provider_broker_or_archive_execution_imports():
    directory = Path(__file__).resolve().parents[3] / "trading_research/v2"
    forbidden = {"alpaca", "requests", "httpx", "urllib", "socket"}
    forbidden_fragments = ("broker", "downloader", "archive", "providers", "trading_runtime.execution")
    for p in directory.glob("*.py"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert all(n.split(".")[0] not in forbidden and not any(x in n for x in forbidden_fragments) for n in names), p.name


def test_invariant_spec_is_not_mutated_by_callers(spec):
    original = digest(spec.base)
    base = spec.base
    base["costs"]["BASELINE"]["slippage_bps"] = 0
    assert digest(spec.base) == original
    assert original == PINS["base_protocol_hash"]


def test_invariant_no_direct_float_decimal_in_production():
    # Runtime golden tests demonstrate the boundary; static check prohibits bypass APIs.
    directory = Path(__file__).resolve().parents[3] / "trading_research/v2"
    for p in directory.glob("*.py"):
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                assert n.func.attr != "from_float"
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Decimal":
                assert not any(isinstance(a, ast.Constant) and type(a.value) is float for a in n.args)
                assert not any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "float" for a in n.args)


def test_invariant_original_authority_file_bytes(spec, spec_dir):
    project = spec_dir.parents[1]
    for name, expected in spec.base["authority_file_hashes"].items():
        assert hashlib.sha256((project/name).read_bytes()).hexdigest() == expected


@pytest.fixture(scope="session")
def bound_metadata(spec, spec_dir):
    root = spec_dir.parents[4] / "aist-data" / "ai-stock-trader"
    b = spec.base["bindings"]
    parent = root / "datasets" / b["parent_dataset_id"] / b["parent_snapshot_id"]
    paths = [root / "research-views" / b["child_view_id"] / "view.json",
             parent / "manifest.json", parent / "checksums.json"]
    return tuple(read_json(metadata_path(p)) for p in paths)


@pytest.mark.parametrize("key", ["child_view_id","parent_snapshot_id","parent_dataset_id","universe_hash",
                                "quality_mask_hash","halt_calendar_hash","action_overlay_hash",
                                "parent_manifest_hash","parent_checksums_hash"])
def test_invariant_data_binding_mismatch(spec, bound_metadata, key):
    from trading_research.v2_protocol import verify_bindings
    p = spec.base
    p["bindings"][key] = "0"*64
    with pytest.raises(SafetyError, match="BINDING"):
        verify_bindings(p, *bound_metadata)


def test_golden_bound_metadata_without_price_rows(spec, spec_dir, bound_metadata):
    from trading_research.v2_protocol import verify_bindings
    assert verify_bindings(spec.base, *bound_metadata) == "BINDINGS_PASS"
    assert validate_metadata(spec_dir.parents[1], spec_dir.parents[4]/"aist-data").config() == spec.config()


def test_invariant_metadata_read_allowlist(spec_dir, monkeypatch):
    import trading_research.v2.runner as runner
    actual = runner.read_json
    seen = []
    def read(path):
        assert path.name in {"view.json","manifest.json","checksums.json"}
        seen.append(path.name)
        return actual(path)
    monkeypatch.setattr(runner, "read_json", read)
    validate_metadata(spec_dir.parents[1], spec_dir.parents[4]/"aist-data")
    assert seen == ["view.json","manifest.json","checksums.json"]


def test_invariant_parent_child_metadata_not_mutated(spec, spec_dir, bound_metadata):
    before = digest(bound_metadata)
    spec.verify_metadata(spec_dir.parents[1], *bound_metadata)
    assert digest(bound_metadata) == before


def test_golden_committed_hash_bytes(monkeypatch):
    from trading_research.v2.conformance import implementation_files
    import trading_research.v2.conformance as conformance
    root = Path(__file__).resolve().parents[3]
    def git_blob(args, **kwargs):
        return (root / args[-1].split(':', 1)[1]).read_bytes().replace(b'\r\n', b'\n')
    monkeypatch.setattr(conformance.subprocess, 'check_output', git_blob)
    result = implementation_files(root, committed=True)
    assert result['trading_runtime/config.py'] == hashlib.sha256((root/'trading_runtime/config.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def test_invariant_uncommitted_source_rejects_freeze(monkeypatch):
    from trading_research.v2.conformance import implementation_files
    import trading_research.v2.conformance as conformance
    monkeypatch.setattr(conformance.subprocess, 'check_output', lambda *args, **kwargs: b'other source')
    with pytest.raises(ValueError, match='COMMITTED_SOURCE'):
        implementation_files(Path(__file__).resolve().parents[3], committed=True)
