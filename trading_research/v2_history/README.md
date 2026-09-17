# Frozen development adapter

This layer maps the immutable B1 regular-session monthly objects and B2 quality
metadata to the unchanged D1R computational core. The sole price window is
2016-01-04..2021-12-31. Later objects are excluded using metadata before opening.
The hash-bound plan defines symbol/month IDs; the checksum-bound regular-session
calendar proves the sessions within each object. A month with any scheduled
session outside the authorized interval is rejected. No read-then-filter path
exists. Exact canonical OHLCV decimal strings are retained.

Phase A uses generated fixtures and metadata only. `preflight` cannot open bars.
The operator must commit/push the adapter and a private zero-exposure manifest,
then verify both remotes before invoking `run-development --pre-commit SHA`.
The runner verifies the committed manifest, remote-tracking commit, code/config/
dependency/data identities and partition-plan hash before enabling price reads.
Its process audit hook denies network and every unselected archive price path.
No provider or broker adapter is used.

Phase B decodes only selected monthly objects, validates their hashes and full
structural integrity, and records a structural audit before constructing any
HistoricalDevelopmentSession. A structural defect stops the run; there is no
same-run repair. After that audit, all 26 development slots execute using the
unchanged core. Subperiods reuse the same STRESS records. Bootstrap uses the core
seed/indices/algorithm. JSON represents negative-infinite bootstrap lower bounds
as the explicit string `-Infinity`; gates receive the original float value.

The D1R ledger hashes the `purpose` field. D2 puts canonical JSON containing the
adapter and combined runtime hashes in that field, binding them without changing
the frozen ledger or creating extra slots. Each attempt records source provenance
and cumulative rows exposed by that attempt. The completion additionally records
global unique `(parent_snapshot_id, symbol, UTC start)` row count; the parent is
fixed for the entire run. Decoded counts increment for every canonical row parsed;
exposure counts increment for every row handed to a HistoricalDevelopmentSession,
including repeated use in separate registered evaluations. A metadata marker is
written immediately before the first core evaluation. Generated test evidence
is confined to pytest temporary storage and is not historical exposure evidence.

No existing attempt is silently rerun. Interrupted evidence remains append-only;
exact recovery requires explicit inspection/authorization, the same trial identity
and a new attempt number. Changed code/config cannot reuse a consumed slot.

Outcome classification is mechanical: integrity failures are INVALID; all gates
passing is PASS; failures confined to trade/sample counts are MORE_DATA_REQUIRED;
any other frozen performance/robustness failure is NO_GO. Mixed sample and
performance failures are NO_GO. No positivity criterion is reclassified after
results. All passed and failed gate records are retained.

Commands from the public repository:

    ../.venv/Scripts/python.exe -m trading_research.v2_history.development_runner preflight --project ../second-brain/projects/ai-stock-trader --data-root ../aist-data

    ../.venv/Scripts/python.exe -m trading_research.v2_history.development_runner run-development --project ../second-brain/projects/ai-stock-trader --data-root ../aist-data --pre-commit <published-private-manifest-commit>

The second command requires the separately authorized D2 phase and published
zero-exposure barrier. Validation, holdout, OOS, Paper and live remain unavailable.
