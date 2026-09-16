"""Append-only, hash-chained provenance-bound trials and frozen slot identities.

No stage execution lives here. An interrupted atomic event temporary is ignored;
committed events are checked as a complete consecutive chain before every append.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import uuid

from trading_runtime.config import SafetyError
from .bindings import PINS, canonical, digest, read_json, require_pins
from .provenance import Provenance, require_provenance, require_fixture_kind, exposure_count

SOURCE = "SYNTHETIC_GOLDEN_D1"
HASH_FIELDS = ("implementation_hash", "execution_config_hash", "dependency_fingerprint",
               "child_view_id", "parent_snapshot_id")


def schedule(spec):
    """Exactly the 53 registered slots, including records that are not candidates."""
    base = spec.base
    canonical_id = base["selection"]["canonical_id"]
    all_variants = [v["id"] for v in base["hypotheses"][0]["variants"]]
    slots = []
    for stage in ("development", "validation", "internal_holdout", "external_oos"):
        variants = all_variants if stage == "development" else [canonical_id]
        for variant in variants:
            for cost in sorted(base["costs"]):
                slots.append((stage, variant, cost, "STRATEGY", 5, None))
            slots.append((stage, variant, "STRESS", "BOOTSTRAP", 5, None))
            if stage == "development":
                periods = re.findall(r"\d{4}-\d{4}", base["acceptance"][stage]["subperiods"])
                slots.extend((stage, variant, "STRESS", "SUBPERIOD", 5, p) for p in periods)
        slots.extend([
            (stage, canonical_id, "STRESS", "DELAY_DIAGNOSTIC", 10, None),
            (stage, canonical_id, "NONE", "NO_TRADE", 0, None),
        ])
        slots.extend((stage, canonical_id, c, "TIME_MATCHED_SPY", 5, None) for c in sorted(base["costs"]))
    if len(slots) != base["trial_budget"]["max_evaluation_records"]:
        raise SafetyError("D1_TRIAL_SCHEDULE")
    return tuple(slots)


def slot_of(value):
    return tuple(value.get(k) for k in ("stage", "variant", "cost_scenario", "record_type", "delay_minutes", "subperiod"))


def trial_identity(value, spec, *, provenance=Provenance.SYNTHETIC, fixture_kind=None):
    mode = require_provenance(provenance, stage=value.get("stage"))
    require_fixture_kind(fixture_kind)
    if value.get("provenance", mode.value) != mode.value or value.get("fixture_kind", fixture_kind) != fixture_kind:
        raise SafetyError("D1R_TRIAL_PROVENANCE_MISMATCH")
    require_pins(value)
    for key in HASH_FIELDS:
        if not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise SafetyError("D1_TRIAL_HASH")
    for key in ("child_view_id", "parent_snapshot_id"):
        if value[key] != spec.base["bindings"][key]:
            raise SafetyError("D1_BINDING_MISMATCH")
    if slot_of(value) not in schedule(spec):
        raise SafetyError("D1_UNREGISTERED_TRIAL")
    if value.get("family") != spec.base["hypotheses"][0]["id"] or not value.get("purpose"):
        raise SafetyError("D1_TRIAL_PURPOSE")
    seed = spec.base["bootstrap"]["seed"] if value["record_type"] == "BOOTSTRAP" else None
    if value.get("seed") != seed:
        raise SafetyError("D1_TRIAL_SEED")
    keys = (*PINS, *HASH_FIELDS, "family", "variant", "stage", "cost_scenario",
            "record_type", "delay_minutes", "subperiod", "seed", "purpose")
    identity = {k: value.get(k) for k in keys}
    identity.update(provenance=mode.value, fixture_kind=fixture_kind)
    return digest(identity), identity


class Ledger:
    """One provenance namespace per ledger; historical slots are development only."""

    def __init__(self, directory, spec, *, provenance=Provenance.SYNTHETIC, fixture_kind=None):
        self.directory = Path(directory)
        self.spec = spec
        self._provenance = require_provenance(provenance, stage="development")
        self.fixture_kind = require_fixture_kind(fixture_kind)

    @property
    def provenance(self):
        return self._provenance

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / ".writer.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise SafetyError("D1_LEDGER_WRITER_ACTIVE") from None
        try:
            yield
        finally:
            os.close(fd)
            path.unlink()

    def read(self):
        result = []
        previous = None
        for index, path in enumerate(sorted(self.directory.glob("event-*.json"))):
            event = read_json(path)
            expected = digest({k: v for k, v in event.items() if k != "event_hash"})
            if (path.name != f"event-{index:08d}.json" or event.get("sequence") != index
                    or event.get("previous_event_hash") != previous or event.get("event_hash") != expected
                    or event.get("source") != self.provenance.value
                    or event.get("fixture_kind") != self.fixture_kind
                    or event.get("ledger_schema_version") != 2):
                raise SafetyError("D1_LEDGER_CORRUPT")
            exposure_count(self.provenance, event.get("historical_price_rows_exposed"))
            require_pins(event)
            if trial_identity(event["identity"], self.spec, provenance=self.provenance,
                              fixture_kind=self.fixture_kind)[0] != event["trial_id"]:
                raise SafetyError("D1_LEDGER_CORRUPT")
            result.append(event)
            previous = expected
        return result

    def _append(self, events, body):
        event = {**PINS, **body, "source": self.provenance.value,
                 "fixture_kind": self.fixture_kind, "ledger_schema_version": 2,
                 "exposure_accounting_scope": self.fixture_kind or "ACTUAL_EXECUTION",
                 "sequence": len(events),
                 "previous_event_hash": events[-1]["event_hash"] if events else None}
        event["event_hash"] = digest(event)
        target = self.directory / f"event-{len(events):08d}.json"
        temporary = self.directory / f".partial-{uuid.uuid4().hex}"
        data = canonical(event) + b"\n"
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise SafetyError("D1_LEDGER_OVERWRITE")
        # Exclusive writer lock + fresh monotonically numbered destination.
        temporary.rename(target)
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest():
            raise SafetyError("D1_LEDGER_WRITE")
        return event

    def begin(self, value, *, source, historical_price_rows_exposed=None):
        mode = require_provenance(source, stage=value.get("stage"))
        if mode is not self.provenance:
            raise SafetyError("D1_REAL_DATA_FORBIDDEN")
        count = exposure_count(mode, historical_price_rows_exposed)
        trial_id, identity = trial_identity(value, self.spec, provenance=mode, fixture_kind=self.fixture_kind)
        with self._lock():
            events = self.read()
            trials = {e["trial_id"]: e["identity"] for e in events if e["status"] == "ATTEMPT_STARTED"}
            if trial_id not in trials:
                if len(trials) >= self.spec.base["trial_budget"]["max_evaluation_records"]:
                    raise SafetyError("D1_TRIAL_BUDGET_OVERFLOW")
                # Unused slots cannot fund extra code/config alternatives for a used slot.
                if any(slot_of(v) == slot_of(identity) for v in trials.values()):
                    raise SafetyError("D1_TRIAL_SLOT_ALREADY_CONSUMED")
            attempt = sum(e["trial_id"] == trial_id and e["status"] == "ATTEMPT_STARTED" for e in events) + 1
            return self._append(events, {"trial_id": trial_id, "identity": identity,
                                       "attempt": attempt, "status": "ATTEMPT_STARTED",
                                       "historical_price_rows_exposed": count})

    def finish(self, trial_id, attempt, *, status, outcome_hash, historical_price_rows_exposed=None):
        count = exposure_count(self.provenance, historical_price_rows_exposed)
        if status not in {"PASS", "FAIL", "EMPTY", "INTERRUPTED"} or re.fullmatch(r"[0-9a-f]{64}", outcome_hash) is None:
            raise SafetyError("D1_LEDGER_OUTCOME")
        with self._lock():
            events = self.read()
            matches = [e for e in events if e["trial_id"] == trial_id and e["attempt"] == attempt]
            if len(matches) != 1 or matches[0]["status"] != "ATTEMPT_STARTED":
                raise SafetyError("D1_LEDGER_ATTEMPT")
            if count < matches[0]["historical_price_rows_exposed"]:
                raise SafetyError("D1R_EXPOSURE_CANNOT_DECREASE")
            return self._append(events, {"trial_id": trial_id, "identity": matches[0]["identity"],
                                       "attempt": attempt, "status": status, "outcome_hash": outcome_hash,
                                       "historical_price_rows_exposed": count})
