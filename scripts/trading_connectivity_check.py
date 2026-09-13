"""Paper read-only connectivity check; no order submission."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_runtime.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["connectivity", *sys.argv[1:]]))
