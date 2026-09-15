"""Bounded B2 GET evidence runner; reuses the hidden local credential mechanism."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import gzip
from io import StringIO
import json
import logging
import os
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.trading_dataset_local_run import credentials, hidden_prompt, response_guard  # noqa: E402
from trading_research.archive import (
    atomic_json,
    data_root,
    file_hash,
    filesystem_path,
    now,
)  # noqa: E402
from trading_research.data_quality import utc_stamp  # noqa: E402
from trading_research.dataset_cli import local_safety  # noqa: E402
from trading_research.dataset_manifest import canonical, sha256  # noqa: E402
from trading_research.providers.alpaca import AlpacaFoundationProvider, ROUTES  # noqa: E402
from trading_research.remediation import validate_query  # noqa: E402


def run(plan, root, config, client):
    if len(plan["queries"]) > 40:
        raise ValueError("QUERY_BUDGET")
    for query in plan["queries"]:
        validate_query(query)
    directory = (
        filesystem_path(data_root({"AIST_DATA_ROOT": str(root)}))
        / "ai-stock-trader/remediation-v2/queries"
        / sha256(plan)
    )
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / "receipts.jsonl"

    def append(value):
        with receipt_path.open("ab") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    old = (
        [json.loads(line) for line in receipt_path.read_text().splitlines()]
        if receipt_path.exists()
        else []
    )
    # Reservations survive process death, so uncertain attempts never evade the cap.
    reserved = sum(r.get("reserved_attempts", 0) for r in old)
    outcomes = []
    for query in plan["queries"]:
        query_id = sha256(query)
        folder = directory / query_id
        folder.mkdir(exist_ok=True)
        marker = folder / "complete.json"
        if marker.exists():
            saved = json.loads(marker.read_text())
            if any(file_hash(folder / n) != h for n, h in saved["files"].items()):
                raise ValueError("EVIDENCE_HASH_MISMATCH")
            outcomes.append(saved)
            continue
        pages, files, rows_total, next_cursor, status = [], {}, 0, None, "COMPLETE"
        for page_no in range(3):
            params = dict(query["params"])
            if next_cursor:
                params["page_token"] = next_cursor
            budget = min(3, 80 - reserved)
            if budget < 1:
                status = "BUDGET_EXHAUSTED"
                break
            reserved += budget
            append(
                {
                    "event": "REQUEST_START",
                    "time": now(),
                    "query_id": query_id,
                    "route": query["route"],
                    "endpoint": ROUTES[query["route"]],
                    "params": params,
                    "purpose": query["purpose"],
                    "reserved_attempts": budget,
                }
            )
            provider = AlpacaFoundationProvider(
                config, None, client=client, request_budget=budget
            )
            body = None
            try:
                body = provider.get(query["route"], params)
                field = query["route"]
                rows = (body.get(field) or {}).get(params["symbols"], [])
                if set(body.get(field) or {}) - {params["symbols"]}:
                    raise ValueError("UNEXPECTED_SYMBOL")
                if any(
                    not utc_stamp(params["start"])
                    <= utc_stamp(r["t"])
                    <= utc_stamp(params["end"])
                    or utc_stamp(r["t"]).year >= 2025
                    for r in rows
                ):
                    raise ValueError("REMEDIATION_BOUNDARY")
                payload = {
                    "query": query,
                    "page": page_no,
                    "retrieved_at": now(),
                    "body": body,
                }
                filename = sha256(payload) + ".json.gz"
                target = folder / filename
                if not target.exists():
                    temporary = folder / (".partial-" + filename)
                    temporary.write_bytes(gzip.compress(canonical(payload), mtime=0))
                    os.replace(temporary, target)
                files[filename] = file_hash(target)
                pages.append(
                    {
                        "file": filename,
                        "rows": len(rows),
                        "timestamps": [r["t"] for r in rows],
                        "content_hash": sha256(body),
                    }
                )
                rows_total += len(rows)
                next_cursor = body.get("next_page_token")
            except Exception:
                status = "REQUEST_FAILED"
            finally:
                append(
                    {
                        "event": "REQUEST_FINISH",
                        "time": now(),
                        "query_id": query_id,
                        "attempts": provider.receipts,
                        "status": status,
                    }
                )
            if any(r.get("http_status") == 401 for r in provider.receipts):
                raise ValueError("AUTHENTICATION_REQUIRED")
            if status != "COMPLETE" or not next_cursor:
                break
        if next_cursor and status == "COMPLETE":
            status = "TRUNCATED_EVIDENCE"
        saved = {
            "query_id": query_id,
            "query": query,
            "status": status,
            "pages": pages,
            "rows": rows_total,
            "files": files,
        }
        atomic_json(marker, saved)
        outcomes.append(saved)
    result = {
        "query_plan_hash": sha256(plan),
        "outcomes": outcomes,
        "reserved_attempts": reserved,
        "oos_requests": 0,
        "strategy_return_calculations": 0,
    }
    atomic_json(directory / "result.json", result)
    return directory, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    project = (
        Path(__file__).resolve().parents[2] / "second-brain/projects/ai-stock-trader"
    )
    status_path = (
        filesystem_path(data_root())
        / "ai-stock-trader/remediation-v2/local-status.json"
    )
    prior = logging.root.manager.disable
    try:
        local_safety(project)
        plan = json.loads(args.plan.read_text())
        for query in plan["queries"]:
            validate_query(query)
        if (
            file_hash(project / "research-v2/remediation-v2/REMEDIATION_PROTOCOL_V2.md")
            != plan["protocol_hash"]
        ):
            raise ValueError("PROTOCOL_HASH_MISMATCH")
        atomic_json(
            status_path,
            {
                "status": "WAITING_FOR_LOCAL_CREDENTIALS",
                "query_plan_hash": sha256(plan),
            },
        )
        print(
            "B2 targeted pre-2025 evidence only. Credentials stay hidden in this process.",
            flush=True,
        )
        config = credentials(os.environ, hidden_prompt)
        atomic_json(status_path, {"status": "RUNNING"})
        logging.disable(sys.maxsize)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with httpx.Client(
                timeout=40,
                trust_env=False,
                follow_redirects=False,
                event_hooks={"response": [response_guard((config.key, config.secret))]},
            ) as client:
                directory, result = run(plan, data_root(), config, client)
        local_safety(project)
        atomic_json(
            status_path,
            {
                "status": "FINISHED",
                "result_directory": str(directory),
                "query_definitions": len(result["outcomes"]),
            },
        )
        print("TARGETED_EVIDENCE_FINISHED", flush=True)
    except (KeyboardInterrupt, EOFError):
        atomic_json(status_path, {"status": "CANCELLED"})
        print("CANCELLED", flush=True)
    except Exception:
        atomic_json(status_path, {"status": "FAILED_SAFE"})
        print("FAILED_SAFE: no exception or credential content displayed", flush=True)
    finally:
        logging.disable(prior)


if __name__ == "__main__":
    main()
