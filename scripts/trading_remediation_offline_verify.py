"""Fresh-process child metadata verification with network/provider access blocked."""

import argparse
import importlib.abc
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--view-id", required=True)
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
                    "trading_research.candidates",
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
        from trading_research.archive import atomic_json, filesystem_path, file_hash
        from trading_research.remediation import verify_view, validity
        from trading_research.restore import read_jsonl
        from trading_research.dataset_manifest import sha256

        if len(args.view_id) != 64 or set(args.view_id) - set("0123456789abcdef"):
            raise ValueError()
        root = filesystem_path(args.root)
        directory = root / "ai-stock-trader/research-views" / args.view_id
        payload = json.loads((directory / "view.json").read_text())
        for key in ("parent_dataset_id", "parent_snapshot_id"):
            if len(payload[key]) != 64 or set(payload[key]) - set("0123456789abcdef"):
                raise ValueError()
        parent = (
            root
            / "ai-stock-trader/datasets"
            / payload["parent_dataset_id"]
            / payload["parent_snapshot_id"]
        )
        result = verify_view(directory, parent)
        calendar = read_jsonl(parent / "calendar/sessions.jsonl.gz")
        mask = payload["mask"]
        results = {}
        # Every parent gap must be outside the valid child domain.
        for item in mask["classifications"]:
            status = validity(mask, item["symbol"], item["timestamp"], calendar)
            if status == "VALID":
                raise ValueError()
            results[status] = results.get(status, 0) + 1
        if any(counters.values()) or mask["fills"] or mask["interpolation"]:
            raise ValueError()
        result.update(counters)
        result.update(
            fresh_process=True,
            credentials_required=False,
            rejected_gap_slots=results,
            mask_hash=sha256(mask),
            universe_hash=sha256(mask["universe"]),
            view_file_hash=file_hash(directory / "view.json"),
            parent_object_verification="Separate full network-blocked parent restore required",
        )
        atomic_json(filesystem_path(args.output), result)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"result": "VIEW_VERIFY_FAIL", **counters}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
