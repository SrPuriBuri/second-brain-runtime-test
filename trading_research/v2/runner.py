"""D1 metadata validation and synthetic conformance; no historical execution command."""

import argparse
import json
import os
from pathlib import Path

from trading_runtime.config import SafetyError
from .bindings import PINS, Spec, canonical, read_json


def require_command(command):
    if command not in {"validate", "conformance"}:
        raise SafetyError("D1_COMMAND_FORBIDDEN")


def metadata_path(path):
    resolved = str(Path(path).resolve())
    return Path("\\\\?\\" + resolved) if os.name == "nt" and not resolved.startswith("\\\\?\\") else Path(resolved)


def validate_metadata(project, root):
    project = Path(project)
    spec = Spec(project / "research-v2/protocol-v2")
    b = spec.base["bindings"]
    root = Path(root) / "ai-stock-trader"
    parent = root / "datasets" / b["parent_dataset_id"] / b["parent_snapshot_id"]
    view = root / "research-views" / b["child_view_id"] / "view.json"
    spec.verify_metadata(project, read_json(metadata_path(view)),
                         read_json(metadata_path(parent / "manifest.json")),
                         read_json(metadata_path(parent / "checksums.json")))
    return spec


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "conformance"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True, help="Metadata only; no price object is opened.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--committed", action="store_true", help="Hash actual HEAD blobs and reject source differences.")
    args = parser.parse_args(argv)
    require_command(args.command)
    try:
        spec = validate_metadata(args.project, args.data_root)
        if args.command == "conformance":
            from .conformance import run
            result = run(spec, args.project / "research-v2/protocol-v2", Path(__file__).resolve().parents[2],
                         committed=args.committed)
        else:
            result = {**PINS, "status": "PASS", "data_bindings": "PASS", "safety_bindings": "PASS",
                      "real_price_rows_read": 0, "execution_available": False}
        if args.output:
            # Never overwrite prior conformance evidence.
            with args.output.open("xb") as handle:
                handle.write(canonical(result) + b"\n")
        print(json.dumps({k: v for k, v in result.items() if k not in {"tests", "implementation_files", "dependencies"}}, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1
    except (SafetyError, OSError, ValueError):
        print('{"status":"FAIL","reason":"D1_VALIDATION_OR_EVIDENCE_ERROR"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
