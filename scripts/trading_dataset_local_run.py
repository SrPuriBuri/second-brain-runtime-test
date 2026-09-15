"""Interactive local data acquisition. Credentials never leave process memory."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import getpass
from io import StringIO
import json
import logging
import os
from pathlib import Path
import sys
import warnings

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_research.archive import (  # noqa: E402
    Archive,
    atomic_json,
    data_root,
    require_retention,
)
from trading_research.dataset_cli import local_safety, main as dataset_main  # noqa: E402
from trading_runtime.config import Config, SafetyError  # noqa: E402

RUNTIME = Path(__file__).resolve().parents[1]
PROJECT = RUNTIME.parent / "second-brain/projects/ai-stock-trader"
KEY_NAMES = ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY")
SAFE_ERRORS = {
    "STORAGE_TERMS_UNRESOLVED",
    "ARCHIVE_SAFETY_INVALID",
    "MISSING_PAPER_CREDENTIALS",
    "FORBIDDEN_BROKER_CONFIGURATION",
    "NOT_PAPER",
    "PROVIDER_SECRET_ECHO_REJECTED",
    "FOUNDATION_TRANSPORT_ERROR",
    "FOUNDATION_REQUEST_BUDGET",
    "ARCHIVE_WRITER_BUSY",
    "FOUNDATION_PRICE_WINDOW_FORBIDDEN",
    "ARCHIVE_OOS_ROW_REJECTED",
    "ARCHIVE_PLAN_HASH_MISMATCH",
    "ARCHIVE_PLAN_CONFLICT",
    "ARCHIVE_SPEC_INVALID",
    "DURABLE_ROOT_INSIDE_REPOSITORY",
    "DURABLE_ROOT_INSIDE_GIT",
} | {
    "FOUNDATION_HTTP_" + str(code)
    for code in (400, 401, 403, 404, 422, 429, 500, 502, 503, 504)
}


def hidden_prompt(label):
    # getpass must never silently fall back to echoed input in a pipe/IDE.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


def credentials(env, prompt):
    memory = dict(env)
    for name, label in zip(
        KEY_NAMES,
        ("Alpaca Paper API key (hidden): ", "Alpaca Paper secret key (hidden): "),
    ):
        if not memory.get(name):
            memory[name] = prompt(label)
        if not memory[name] or not memory[name].strip():
            raise SafetyError("MISSING_PAPER_CREDENTIALS")
    return Config.from_env(memory)


def response_guard(values):
    def guard(response):
        response.read()
        # Scan parsed JSON too, so escaped credentials cannot reach native pages.
        try:
            body = json.dumps(response.json(), ensure_ascii=False)
        except (ValueError, UnicodeError):
            body = response.text
        if any(value and value in body for value in values):
            raise SafetyError("PROVIDER_SECRET_ECHO_REJECTED")

    return guard


def main(argv=None, *, env=None, prompt=None, run=None, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--retention", type=Path)
    parser.add_argument("--max-chunks", type=int, default=1512)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    prompt = hidden_prompt if prompt is None else prompt
    run = dataset_main if run is None else run
    client_factory = httpx.Client if client_factory is None else client_factory
    status_path = None
    prior_logging = logging.root.manager.disable

    def status(state, code=None, reason=None):
        if status_path:
            atomic_json(
                status_path, {"status": state, "exit_code": code, "reason": reason}
            )

    try:
        local_safety(args.project)
        base = args.project / "research-v2/dataset-v2"
        plan_path = args.plan or base / "ACQUISITION_PLAN_V2.json"
        retention_path = args.retention or base / "RETENTION_REVIEW_V2.json"
        require_retention(json.loads(retention_path.read_text(encoding="utf-8")))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        archive = Archive(data_root(env), plan)
        if not 1 <= args.max_chunks <= 2000:
            raise SafetyError("ARCHIVE_CHUNK_BUDGET_INVALID")
        status_path = archive.path / "local-run-status.json"
        print("Private local historical SIP acquisition; price dates end in 2024.")
        print("Credentials are hidden and held only in this process. Ctrl+C cancels.")
        print(json.dumps(archive.status()), flush=True)
        status("WAITING_FOR_LOCAL_CREDENTIALS")
        config = credentials(env, prompt)
        status("RUNNING")
        # Suppress preconfigured HTTP logging handlers, including file handlers.
        logging.disable(sys.maxsize)
        captured = StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            with client_factory(
                timeout=40,
                trust_env=False,
                follow_redirects=False,
                event_hooks={"response": [response_guard((config.key, config.secret))]},
            ) as client:
                result = run(
                    [
                        "dataset-resume",
                        "--project",
                        str(args.project),
                        "--plan",
                        str(plan_path),
                        "--retention",
                        str(retention_path),
                        "--max-chunks",
                        str(args.max_chunks),
                    ],
                    config=config,
                    client=client,
                )
        # Never replay arbitrary captured strings or exception content to a console.
        local_safety(args.project)
        reason = None
        for line in captured.getvalue().splitlines():
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            candidate = value.get("reason") if isinstance(value, dict) else None
            if (
                isinstance(candidate, str)
                and candidate in SAFE_ERRORS
                and config.key not in candidate
                and config.secret not in candidate
            ):
                reason = candidate
        status(
            "ACQUISITION_FINISHED" if result == 0 else "ACQUISITION_FAILED",
            result,
            reason,
        )
        print(
            json.dumps(
                {
                    "status": "ACQUISITION_FINISHED"
                    if result == 0
                    else "ACQUISITION_FAILED",
                    "exit_code": result,
                    "reason": reason,
                }
            )
        )
        print(json.dumps(archive.status()))
        return result
    except (KeyboardInterrupt, EOFError):
        status("CANCELLED", 130)
        print("CANCELLED: completed pages and chunks remain resumable.")
        return 130
    except getpass.GetPassWarning:
        status("SECURE_CONSOLE_REQUIRED", 1)
        print("SECURE_CONSOLE_REQUIRED: run in a native local terminal.")
        return 1
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, SafetyError) and str(exc) in SAFE_ERRORS
            else None
        )
        status("LOCAL_ACQUISITION_FAILED", 1, reason)
        print(json.dumps({"status": "LOCAL_ACQUISITION_FAILED", "reason": reason}))
        return 1
    finally:
        logging.disable(prior_logging)


if __name__ == "__main__":
    code = main()
    if sys.stdin.isatty():
        try:
            input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(code)
