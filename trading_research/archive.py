"""Durable data-only plans, chunks and immutable local snapshots outside git."""

from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

from trading_runtime.config import SafetyError
from .data_quality import pre_oos_interval, utc_stamp
from .dataset_cache import DatasetCache
from .dataset_manifest import canonical, sha256

CORE = (
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLB",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLRE",
    "XLU",
    "XLV",
    "XLY",
)
ACCEPTANCE = {
    "min_symbol_completeness": 0.999,
    "max_unexplained_consecutive_missing_slots": 2,
    "duplicates": 0,
    "out_of_order": 0,
    "invalid_timestamp": 0,
    "invalid_ohlcv": 0,
    "off_grid": 0,
    "unresolved_material_actions": 0,
    "durable_restore_required": True,
    "OOS_price_requests": 0,
    "strategy_return_calculations": 0,
    "suspicious_overnight_ratio_threshold": 0.20,
    "split_ratio_relative_tolerance": 0.10,
}


def now():
    return datetime.now(timezone.utc).isoformat()


def data_root(env=None):
    env = os.environ if env is None else env
    runtime = Path(__file__).resolve().parents[1]
    path = Path(env.get("AIST_DATA_ROOT") or runtime.parent / "aist-data").resolve()
    for repo in (runtime, runtime.parent / "second-brain"):
        if path == repo.resolve() or path.is_relative_to(repo.resolve()):
            raise SafetyError("DURABLE_ROOT_INSIDE_REPOSITORY")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise SafetyError("DURABLE_ROOT_INSIDE_GIT")
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def filesystem_path(path):
    """Use the Windows extended path namespace for nested SHA-256 objects."""
    resolved = str(Path(path).resolve())
    if os.name == "nt" and not resolved.startswith("\\\\?\\"):
        resolved = (
            "\\\\?\\UNC\\" + resolved[2:]
            if resolved.startswith("\\\\")
            else "\\\\?\\" + resolved
        )
    return Path(resolved)


def jsonl_bytes(rows):
    return gzip.compress(b"".join(canonical(row) + b"\n" for row in rows), mtime=0)


def normalize_bar(row):
    stamp = utc_stamp(row["t"])
    if stamp.year >= 2025:
        raise SafetyError("ARCHIVE_OOS_ROW_REJECTED")
    result = {"t": stamp.isoformat().replace("+00:00", "Z")}
    for name in ("o", "h", "l", "c", "v"):
        value = Decimal(str(row[name]))
        if not value.is_finite():
            raise SafetyError("ARCHIVE_NONFINITE_VALUE")
        result[name] = format(value.normalize(), "f") if value else "0"
    return result


def make_plan():
    selection = {
        "schema_version": 1,
        "provider": "alpaca",
        "feed": "sip",
        "timeframe": "5Min",
        "session_scope": "REGULAR",
        "timezone": "America/New_York",
        "archive_start": "2016-01-01",
        "archive_end": "2024-12-31",
        "symbols": sorted(CORE),
        "adjustment_modes": ["raw"],
        "asof": "-",
        "chunking": "symbol/month",
        "optional_adjustment_audit": "separate pre-2025 event windows; never merge into raw",
        "acceptance": ACCEPTANCE,
        "universe_selection": "USER_CORE_MARKET_AND_SECTOR_COVERAGE_NO_RETURNS",
    }
    return {
        "dataset_id": sha256(selection),
        "selection": selection,
        "created_at": now(),
        "universe_status": "CANDIDATE_SET_LOCKED_PENDING_IDENTITY_AND_COVERAGE",
        "OOS_price_requests": 0,
        "strategy_return_calculations": 0,
    }


def chunks(plan):
    spec = plan["selection"]
    start, end = (
        date.fromisoformat(spec["archive_start"]),
        date.fromisoformat(spec["archive_end"]),
    )
    pre_oos_interval(str(start), str(end))
    day = start
    while day <= end:
        next_month = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
        last = min(end, next_month - timedelta(days=1))
        for symbol in spec["symbols"]:
            params = dict(
                symbols=symbol,
                start=str(day) + "T00:00:00Z",
                end=str(last) + "T23:59:59Z",
                feed="sip",
                timeframe="5Min",
                adjustment="raw",
                asof="-",
                sort="asc",
                limit=10000,
            )
            yield {"id": sha256({"route": "bars", "params": params}), "params": params}
        day = next_month


def require_retention(evidence):
    if (
        evidence.get("status") != "PERMITTED"
        or evidence.get("provider") != "alpaca"
        or evidence.get("feed") != "sip"
        or evidence.get("private_local_retention") is not True
        or not evidence.get("source_ref")
        or not evidence.get("reviewed_at")
    ):
        raise SafetyError("STORAGE_TERMS_UNRESOLVED")


class Archive:
    def __init__(self, root, plan):
        if sha256(plan["selection"]) != plan["dataset_id"]:
            raise SafetyError("ARCHIVE_PLAN_HASH_MISMATCH")
        spec = plan["selection"]
        if (
            spec.get("provider") != "alpaca"
            or spec.get("feed") != "sip"
            or spec.get("timeframe") != "5Min"
            or spec.get("adjustment_modes") != ["raw"]
            or not spec["symbols"]
            or not set(spec["symbols"]).issubset(CORE)
            or spec.get("acceptance") != ACCEPTANCE
        ):
            raise SafetyError("ARCHIVE_SPEC_INVALID")
        list(chunks(plan))  # Reject a forbidden interval before writing anything.
        self.root = filesystem_path(root)
        self.plan = plan
        self.path = self.root / "ai-stock-trader" / "work" / plan["dataset_id"]
        self.path.mkdir(parents=True, exist_ok=True)
        existing = self.path / "plan.json"
        if existing.exists():
            previous = json.loads(existing.read_text())
            if previous["selection"] != plan["selection"]:
                raise SafetyError("ARCHIVE_PLAN_CONFLICT")
            self.plan = previous
        else:
            atomic_json(existing, plan)
        self.cache = DatasetCache(self.path / "objects")

    @contextmanager
    def writer(self):
        # OS locks release on process death; a stale lock file does not block resume.
        with (self.path / "writer.lock").open("a+b") as handle:
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SafetyError("ARCHIVE_WRITER_BUSY") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def index(self):
        path = self.path / "checkpoint.json"
        return (
            json.loads(path.read_text())
            if path.exists()
            else {
                "requests": {},
                "chunks": {},
                "receipts": [],
                "OOS_price_requests": 0,
                "blocked_oos_requests": 0,
            }
        )

    def checkpoint(self, index):
        atomic_json(self.path / "checkpoint.json", index)

    def save_object(self, domain, identity, payload):
        spec = self.plan["selection"]
        cfg = {
            "source": "alpaca",
            "feed": "sip",
            "symbols": spec["symbols"],
            "universe_version": self.plan["dataset_id"],
            "start": spec["archive_start"],
            "end": spec["archive_end"],
            "timeframe": "5Min",
            "adjustment": "raw",
            "retrieval_parameters": {"identity": identity},
            "corporate_actions_version": "RAW_EVENT_SNAPSHOT_BOUND_AT_FINALIZATION",
            "security_master_version": "FIXED_ETF_IDENTITY_EVIDENCE_REQUIRED",
        }
        meta = self.cache.freeze(
            domain, cfg, payload, retention="PRIVATE_LOCAL_RESEARCH"
        )
        return {
            "domain": domain,
            "dataset_id": meta["dataset_id"],
            "snapshot_id": meta["snapshot_id"],
            "content_sha256": meta["content_sha256"],
        }

    def read_object(self, ref):
        return self.cache.read(ref["domain"], ref["dataset_id"], ref["snapshot_id"])

    def status(self):
        index = self.index()
        for ref in list(index["requests"].values()) + list(index["chunks"].values()):
            self.read_object(ref)
        return {
            "dataset_id": self.plan["dataset_id"],
            "expected_chunks": len(list(chunks(self.plan))),
            "completed_chunks": len(index["chunks"]),
            "completed_request_pages": len(index["requests"]),
            "attempts": len(index["receipts"]),
            "OOS_price_requests": index["OOS_price_requests"],
            "blocked_oos_requests": index["blocked_oos_requests"],
            "strategy_return_calculations": 0,
        }

    def freeze(self, universe, calendar, actions, quality, software_commit):
        with self.writer():
            return self._freeze(universe, calendar, actions, quality, software_commit)

    def _freeze(self, universe, calendar, actions, quality, software_commit):
        retention = json.loads((self.path / "retention.json").read_text())
        require_retention(retention)
        status = self.status()
        if status["completed_chunks"] != status["expected_chunks"]:
            raise SafetyError("ARCHIVE_CHUNKS_INCOMPLETE")
        if not quality["accepted"] or quality["identity_verified"] is not True:
            raise SafetyError("ARCHIVE_QUALITY_NOT_ACCEPTED")
        if sorted(universe["symbols"]) != self.plan["selection"]["symbols"]:
            raise SafetyError("ARCHIVE_UNIVERSE_MISMATCH")
        index = self.index()
        parent = self.root / "ai-stock-trader" / "datasets" / self.plan["dataset_id"]
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=parent))

        def put(relative, rows):
            path = temporary / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.write(jsonl_bytes(rows))
                handle.flush()
                os.fsync(handle.fileno())

        for request_id, ref in sorted(index["requests"].items()):
            put(
                "provenance/provider_pages/" + request_id + ".jsonl.gz",
                [self.read_object(ref)],
            )
        for chunk_id, ref in sorted(index["chunks"].items()):
            put("bars/" + chunk_id + ".jsonl.gz", self.read_object(ref)["rows"])
        put("calendar/sessions.jsonl.gz", calendar)
        put("corporate_actions/normalized.jsonl.gz", actions)
        for name, value in (
            ("universe.json", universe),
            ("quality/report.json", quality),
            ("provenance/plan.json", self.plan),
            ("provenance/checkpoint.json", index),
        ):
            atomic_json(temporary / name, value)
        atomic_json(temporary / "provenance/retention.json", retention)
        checksums = {
            str(p.relative_to(temporary)).replace("\\", "/"): file_hash(p)
            for p in sorted(temporary.rglob("*"))
            if p.is_file()
        }
        raw_hashes = {
            k: v
            for k, v in checksums.items()
            if k.startswith("provenance/provider_pages/")
        }
        normalized_hashes = {
            k: v
            for k, v in checksums.items()
            if k.startswith(("bars/", "calendar/", "corporate_actions/"))
        }
        spec = self.plan["selection"]
        manifest = {
            **spec,
            "dataset_id": self.plan["dataset_id"],
            "created_at": self.plan["created_at"],
            "common_coverage_start": quality["common_coverage_start"],
            "frozen_universe": universe,
            "universe_hash": sha256(universe),
            "calendar_hash": sha256(calendar),
            "corporate_action_hash": sha256(actions),
            "raw_content_hashes": raw_hashes,
            "normalized_content_hashes": normalized_hashes,
            "retrieval_parameters": spec,
            "retrieval_software_commit": software_commit,
            "provider_api_version": {"bars": "v2", "corporate_actions": "v1"},
            "quality_report_hash": sha256(quality),
            "aggregate_content_hash": sha256(checksums),
            "OOS_price_requests": status["OOS_price_requests"],
            "strategy_return_calculations": 0,
            "source_provenance": "provenance/provider_pages plus checkpoint receipts",
            "state": "FROZEN",
        }
        manifest["snapshot_id"] = sha256(manifest)
        atomic_json(temporary / "checksums.json", checksums)
        atomic_json(temporary / "manifest.json", manifest)
        from .restore import verify_snapshot

        verify_snapshot(temporary, allow_partial=True)
        target = parent / manifest["snapshot_id"]
        if target.exists():
            verify_snapshot(target)
            # Identical existing frozen object wins. Temporary output remains clearly partial.
            return target
        os.rename(temporary, target)
        return target


def portable_summary(manifest):
    # Canonical private persistence contains metadata, never raw bars/page payloads.
    return {
        key: manifest[key]
        for key in (
            "schema_version",
            "dataset_id",
            "snapshot_id",
            "created_at",
            "provider",
            "feed",
            "timeframe",
            "session_scope",
            "timezone",
            "archive_start",
            "archive_end",
            "common_coverage_start",
            "adjustment_modes",
            "frozen_universe",
            "universe_hash",
            "calendar_hash",
            "corporate_action_hash",
            "raw_content_hashes",
            "normalized_content_hashes",
            "retrieval_parameters",
            "retrieval_software_commit",
            "provider_api_version",
            "quality_report_hash",
            "OOS_price_requests",
            "strategy_return_calculations",
            "source_provenance",
            "aggregate_content_hash",
        )
    }
