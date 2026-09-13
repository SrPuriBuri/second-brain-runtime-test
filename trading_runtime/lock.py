"""Atomic durable claims. No automatic expiry or takeover after a crash."""

from .private_store import Conflict, ROOT


def claim_slot(store, day, slot, record):
    # Version intentionally excluded from claim path: one slot/day across versions.
    path = ROOT + f"data/evidence/claims/{day}_{slot}.json"
    try:
        store.create_append_only_record(path, record)
        return True
    except Conflict:
        return False
