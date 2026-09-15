# B2 data remediation

This tooling creates source-backed market-state metadata and a metadata-only child view of an immutable archive. It never creates, interpolates or forward-fills bars. Strategy research and broker operations are outside its scope.

`remediation.py` expands missing runs across the exchange calendar, correlates simultaneous missing observations, represents verified unscheduled halts, and builds deterministic full-symbol-session masks. Only five-minute slots wholly inside a documented halt are removed from trading expectations. Partial overlaps remain expected. Correlation and empty provider responses never prove a cause.

The frozen B2 inclusion gates are completeness >=99.9% after full verified halt slots but before session exclusion, and excluded sessions <=0.5% of the scheduled sessions. Every remaining non-halt missing observation invalidates its whole symbol-session. A structural failure excludes the instrument only from the child. Parent raw evidence and the original B1 decision remain unchanged.

`validity(mask, symbol, stamp, calendar, window_start=None)` checks five-minute slot starts. Supply `window_start` for every multi-session feature window; a window crossing the XLF in-kind distribution boundary or an invalid session is rejected. A VALID result is a quality-domain check, not a bar or an instruction to manufacture one: consumers must still load an actual checksum-verified parent observation. This phase does not authorize feature or strategy computation.

`freeze_view` atomically creates content-addressed metadata under `AIST_DATA_ROOT/ai-stock-trader/research-views/<view-id>`. `verify_view` checks its identity and pinned parent manifest/checksum files. Use the existing full `verify_snapshot` / network-blocked restore to verify every underlying parent object as well. Repeated identical freezes reuse the same view; conflicting content fails. No method writes into the parent.

The B2-specific assembly command is `python scripts/trading_remediation_finalize.py`, from the public repository with its private sibling and durable data root present. It uses completed local query evidence only, verifies the preregistered protocol hash, and reproduces the JSON metadata. It does not download anything. Human source assessments are explicit inputs in this phase-specific assembly script, not automatic discoveries.

The secure targeted acquisition launcher is `python scripts/trading_remediation_local_run.py --plan <private TARGETED_QUERY_PLAN_V2.json>`. **B2's 23 definitions are already complete; do not repeat acquisition.** It reuses the existing hidden credential mechanism; credentials remain in process memory. Definitions require one symbol, explicit purpose/feed/window, <=24 hours, and a strict pre-2025 boundary. The phase caps definitions at 40, pages at 3 per definition, and network attempts at 80. Receipts reserve attempts before requests, survive interruption and remain outside Git alongside native evidence. A complete marker and matching hashes allow reuse. Empty or truncated evidence cannot be treated as complete market coverage.

Validation:

```powershell
../.venv/Scripts/python.exe -m pytest tests/trading_research tests/trading_runtime -q --basetemp=../.test-tmp/b2-final --tb=short
../.venv/Scripts/python.exe -m ruff check trading_research tests/trading_research trading_runtime tests/trading_runtime scripts/trading_*
```

Unknown causes can remain only outside the valid child domain. The strongest readiness state must not be claimed while those uncertainties remain. This fixed ETF view is not an unbiased point-in-time equity universe and does not establish historical news vintages or universally valid cross-day adjustments.
