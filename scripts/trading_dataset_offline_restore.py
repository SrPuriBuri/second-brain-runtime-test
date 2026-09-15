"""Fresh-process real archive restore with socket and provider imports blocked."""

import argparse
import importlib.abc
import json
import os
from pathlib import Path
import re
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    counters = {"network_attempts": 0, "provider_import_attempts": 0}

    def forbidden(*args, **kwargs):
        counters["network_attempts"] += 1
        raise RuntimeError("OFFLINE_NETWORK_FORBIDDEN")

    class BlockProvider(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(
                (
                    "httpx",
                    "requests",
                    "alpaca",
                    "trading_research.providers",
                    "trading_research.downloader",
                    "trading_research.simulator",
                    "trading_research.experiment",
                )
            ):
                counters["provider_import_attempts"] += 1
                raise RuntimeError("OFFLINE_PROVIDER_FORBIDDEN")

    for key in list(os.environ):
        if key.startswith(("ALPACA", "APCA", "SECOND_BRAIN")):
            os.environ.pop(key, None)
    sys.meta_path.insert(0, BlockProvider())
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.socket.sendto = forbidden
    socket.create_connection = forbidden
    socket.getaddrinfo = forbidden
    try:
        from trading_research.archive import atomic_json, chunks, filesystem_path
        from trading_research.dataset_manifest import sha256
        from trading_research.restore import read_jsonl, verify_snapshot

        if not all(
            re.fullmatch("[a-f0-9]{64}", x) for x in (args.dataset_id, args.snapshot_id)
        ):
            raise ValueError()
        root = filesystem_path(args.root)
        snapshot = (
            root / "ai-stock-trader/datasets" / args.dataset_id / args.snapshot_id
        )
        result = verify_snapshot(snapshot)
        plan = json.loads((snapshot / "provenance/plan.json").read_text())
        years = sorted({c["params"]["start"][:4] for c in chunks(plan)})
        wanted = {years[0], years[len(years) // 2], years[-1]}
        samples = {}
        for chunk in chunks(plan):
            year = chunk["params"]["start"][:4]
            key = chunk["params"]["symbols"] + "/" + year
            if year in wanted and key not in samples:
                rows = read_jsonl(snapshot / "bars" / (chunk["id"] + ".jsonl.gz"))
                if rows:
                    samples[key] = {
                        "chunk_id": chunk["id"],
                        "rows": len(rows),
                        "first_timestamp": rows[0]["t"],
                        "last_timestamp": rows[-1]["t"],
                        "logical_hash": sha256(rows),
                    }
        if len(samples) != len(plan["selection"]["symbols"]) * len(wanted) or any(
            counters.values()
        ):
            raise ValueError()
        result.update(counters)
        result.update(
            fresh_process=True,
            credentials_required=False,
            representative_chunks=samples,
        )
        atomic_json(filesystem_path(args.output), result)
        print(
            json.dumps(
                {k: v for k, v in result.items() if k != "representative_chunks"}
            )
        )
        return 0
    except Exception:
        print(json.dumps({"result": "RESTORE_FAIL", **counters}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
