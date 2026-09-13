"""Use the established in-memory secret-output guard for isolated research."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.trading_safe_run import guarded_run  # noqa: E402
from trading_research.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(guarded_run(sys.argv[1:], run=main))
