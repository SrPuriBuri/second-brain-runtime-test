from .models import Handoff
from .private_store import ROOT


def write_handoff(store, run, now, status, evidence, unresolved, next_action=None):
    record = Handoff(
        run_id=run,
        timestamp=now,
        status=status,
        next_action=next_action
        or "Inspect unresolved issues, reconcile exact broker IDs before recovery; keep strategy and kill switch disabled.",
        evidence_refs=evidence,
        unresolved=unresolved,
    )
    path = ROOT + f"data/handoffs/{run}.json"
    store.create_append_only_record(path, record.model_dump(mode="json"))
    return path
