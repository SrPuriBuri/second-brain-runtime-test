"""D1R provenance and stage/date boundaries; no archive access or strategy logic."""

from datetime import date
from enum import Enum

from trading_runtime.config import SafetyError


class Provenance(str, Enum):
    SYNTHETIC = "SYNTHETIC_GOLDEN_D1"
    DEVELOPMENT = "HISTORICAL_DEVELOPMENT_D2"


GENERATED_FIXTURE = "GENERATED_PROVENANCE_FIXTURE"
DEVELOPMENT_START = date(2016, 1, 4)
DEVELOPMENT_END = date(2021, 12, 31)


def require_provenance(source, *, stage=None):
    try:
        mode = Provenance(source)
    except (ValueError, TypeError):
        raise SafetyError("D1R_UNKNOWN_PROVENANCE_REAL_DATA_FORBIDDEN") from None
    if mode is Provenance.DEVELOPMENT and stage != "development":
        raise SafetyError("D1R_HISTORICAL_STAGE_FORBIDDEN")
    return mode


def require_development_date(value):
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else value
        if type(day) is not date or not DEVELOPMENT_START <= day <= DEVELOPMENT_END:
            raise ValueError()
    except (ValueError, TypeError):
        raise SafetyError("D1R_DEVELOPMENT_DATE_FORBIDDEN") from None
    return day


def require_fixture_kind(value):
    if value not in (None, GENERATED_FIXTURE):
        raise SafetyError("D1R_UNKNOWN_FIXTURE_KIND")
    return value


def require_records(records, *, source=None, stage=None):
    """Homogeneous provenance; empty ledgers use explicit context or D1 default."""
    modes, fixture_kinds = set(), set()
    for record in records:
        mode = require_provenance(record.get("source"), stage=record.get("stage"))
        modes.add(mode)
        if mode is Provenance.DEVELOPMENT:
            require_development_date(record.get("session"))
        fixture_kinds.add(require_fixture_kind(record.get("fixture_kind")))
    if len(modes) > 1:
        raise SafetyError("D1R_MIXED_PROVENANCE")
    if len(fixture_kinds) > 1:
        raise SafetyError("D1R_MIXED_FIXTURE_PROVENANCE")
    mode = next(iter(modes), Provenance.SYNTHETIC)
    if source is not None:
        declared = require_provenance(source, stage=stage)
        if modes and mode is not declared:
            raise SafetyError("D1R_MIXED_PROVENANCE")
        mode = declared
    if stage is not None:
        require_provenance(mode, stage=stage)
    return mode


def exposure_count(mode, value):
    """Explicit per-attempt cumulative rows, supplied by the future runner."""
    if value is None and mode is Provenance.SYNTHETIC:
        value = 0
    if type(value) is not int or value < 0:
        raise SafetyError("D1R_EXPLICIT_NONNEGATIVE_EXPOSURE_REQUIRED")
    if mode is Provenance.SYNTHETIC and value != 0:
        raise SafetyError("D1R_SYNTHETIC_HISTORICAL_EXPOSURE")
    return value
