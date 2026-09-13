"""Append-only event IDs; never overwrite a prior event."""

from uuid import uuid4

from .private_store import ROOT


def event(store, run_id, kind, timestamp, data):
    path = ROOT + f"data/journal/{run_id}/{uuid4().hex}.json"
    store.create_append_only_record(
        path,
        {
            "run_id": run_id,
            "kind": kind,
            "timestamp": timestamp.isoformat(),
            "data": data,
        },
    )
    return path
