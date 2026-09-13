"""Capture job output in memory and prevent raw credentials reaching Actions logs."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_runtime.config import SECRET_NAMES, redact  # noqa: E402


def guarded_run(argv, env=None, run=None):
    if run is None:
        from trading_runtime.cli import main

        run = main
    env = os.environ if env is None else env
    captured = StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        result = run(argv)
    output = captured.getvalue()
    if any(env.get(name) and env[name] in output for name in SECRET_NAMES):
        print('{"status":"FAILED","reason":"OUTPUT_SECRET_LEAK_PREVENTED"}')
        return 1
    print(redact(output, env), end="")
    return result


if __name__ == "__main__":
    raise SystemExit(guarded_run(sys.argv[1:]))
