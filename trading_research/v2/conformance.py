"""Synthetic test evidence and relevant deterministic runtime fingerprints."""

import hashlib
from importlib import metadata, resources
import os
from pathlib import Path
import platform
import subprocess

from .bindings import PINS, digest


def dependencies():
    tz = resources.files("tzdata.zoneinfo.America").joinpath("New_York").read_bytes()
    return {"python": platform.python_version(), "python_implementation": platform.python_implementation(),
            "platform": platform.system(), "machine": platform.machine(),
            "numpy": metadata.version("numpy"), "tzdata": metadata.version("tzdata"),
            "timezone_source": "tzdata.zoneinfo.America/New_York (ZoneInfo.from_file)",
            "timezone_sha256": hashlib.sha256(tz).hexdigest(),
            "conformance_pytest": metadata.version("pytest")}


def implementation_files(root, *, committed=False):
    root = Path(root)
    paths = sorted((root / "trading_research/v2").glob("*.py"))
    # Freeze metadata/safety dependencies used by the V2 package as well.
    paths += [root / p for p in ("trading_research/v2_protocol.py",
                                "trading_research/dataset_manifest.py",
                                "trading_research/data_quality.py",
                                "trading_runtime/config.py", "trading_runtime/private_store.py")]
    result = {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if committed:
            data = subprocess.check_output(["git", "-c", "safe.directory=*", "-C", str(root),
                                            "show", "HEAD:" + name], stderr=subprocess.DEVNULL)
            # Git's CRLF checkout conversion is harmless, but actual source changes are not.
            if data.replace(b"\r\n", b"\n") != path.read_bytes().replace(b"\r\n", b"\n"):
                raise ValueError("D1_COMMITTED_SOURCE_MISMATCH")
        result[name] = hashlib.sha256(data).hexdigest()
    return result


def run(spec, spec_dir, root, *, committed=False):
    """Pytest runs only repository-owned synthetic fixtures, with network blocked."""
    import pytest

    class Evidence:
        def __init__(self):
            self.tests = {}
            self.failures = []

        def pytest_runtest_logreport(self, report):
            if report.when == "call":
                self.tests[report.nodeid] = report.outcome
            elif report.failed:
                self.failures.append({"test": report.nodeid, "phase": report.when})

    plugin = Evidence()
    previous = os.environ.get("AIST_V2_SPEC_DIR")
    os.environ["AIST_V2_SPEC_DIR"] = str(Path(spec_dir).resolve())
    try:
        code = pytest.main([str(Path(root) / "tests/trading_research/v2"), "-q",
                            "-p", "no:cacheprovider", "--tb=short"], plugins=[plugin])
    finally:
        if previous is None:
            os.environ.pop("AIST_V2_SPEC_DIR", None)
        else:
            os.environ["AIST_V2_SPEC_DIR"] = previous
    tests = dict(sorted(plugin.tests.items()))
    deps = dependencies()
    implementation = implementation_files(root, committed=committed)
    config = spec.config()
    return {**PINS, "status": "PASS" if code == 0 and tests and not plugin.failures else "FAIL",
            "source": "SYNTHETIC_GOLDEN_D1", "pytest_exit_code": int(code), "tests": tests,
            "setup_teardown_failures": plugin.failures, "total_tests": len(tests),
            "golden_tests": sum("::test_golden_" in k for k in tests),
            "property_invariant_tests": sum("::test_invariant_" in k for k in tests),
            "implementation_files": implementation, "implementation_hash": digest(implementation),
            "implementation_hash_basis": "GIT_HEAD_BYTES" if committed else "UNFROZEN_WORKTREE_BYTES",
            "execution_config_hash": digest(config), "dependency_fingerprint": digest(deps),
            "dependencies": deps, "data_bindings": spec.base["bindings"],
            "real_historical_V2_calculations": 0, "real_historical_V2_signals": 0,
            "OOS_price_access": 0, "broker_mutations": {"submitted": 0, "cancelled": 0, "closed": 0}}
