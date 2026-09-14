"""Offline snapshot verification. No provider or credential dependency."""

import gzip
import json
from pathlib import Path

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import session_from_row
from .archive import file_hash
from .dataset_manifest import sha256
from .data_quality import utc_stamp


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def verify_snapshot(path, allow_partial=False):
    root = Path(path).resolve()
    try:
        if root.name.startswith(".partial-") and not allow_partial:
            raise ValueError()
        manifest = json.loads((root / "manifest.json").read_text())
        checksums = json.loads((root / "checksums.json").read_text())
        expected = manifest["snapshot_id"]
        if (
            sha256({k: v for k, v in manifest.items() if k != "snapshot_id"})
            != expected
        ):
            raise ValueError()
        if not allow_partial and root.name != expected:
            raise ValueError()
        if sha256(manifest["retrieval_parameters"]) != manifest["dataset_id"]:
            raise ValueError()
        actual_files = {
            p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
        } - {"manifest.json", "checksums.json"}
        if (
            actual_files != set(checksums)
            or sha256(checksums) != manifest["aggregate_content_hash"]
        ):
            raise ValueError()
        for name, digest in checksums.items():
            target = (root / name).resolve()
            if (
                not target.is_relative_to(root)
                or (root / name).is_symlink()
                or file_hash(target) != digest
            ):
                raise ValueError()
        calendar = read_jsonl(root / "calendar/sessions.jsonl.gz")
        actions = read_jsonl(root / "corporate_actions/normalized.jsonl.gz")
        universe = json.loads((root / "universe.json").read_text())
        quality = json.loads((root / "quality/report.json").read_text())
        if (
            sha256(calendar) != manifest["calendar_hash"]
            or sha256(actions) != manifest["corporate_action_hash"]
            or sha256(universe) != manifest["universe_hash"]
            or sha256(quality) != manifest["quality_report_hash"]
        ):
            raise ValueError()
        for row in calendar:
            session_from_row(row)
            if row["date"] >= "2025-01-01":
                raise ValueError()
        count, chunks = 0, 0
        for file in sorted((root / "bars").glob("*.jsonl.gz")):
            rows = read_jsonl(file)
            for row in rows:
                if utc_stamp(row["t"]).year >= 2025:
                    raise ValueError()
            count += len(rows)
            chunks += 1
        if (
            manifest["OOS_price_requests"] != 0
            or manifest["strategy_return_calculations"] != 0
            or manifest["state"] != "FROZEN"
        ):
            raise ValueError()
        return {
            "result": "RESTORE_PASS",
            "snapshot_id": expected,
            "manifest_sha256": file_hash(root / "manifest.json"),
            "aggregate_content_hash": manifest["aggregate_content_hash"],
            "verified_files": len(checksums),
            "bar_chunks": chunks,
            "rows": count,
            "calendar_sessions": len(calendar),
            "corporate_actions": len(actions),
            "provider_calls": 0,
        }
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        raise SafetyError("RESTORE_FAIL") from None
