# D1R: provenance interface revision

This revision preserves the D1 economic and statistical algorithms. The original
D1 implementation remains at commit `24496a5ddc307b29cabc4806ce6423c88261bc52`.
Base and clarifications C1–C4 are unchanged. No historical archive reader or
development runner is included.

`Provenance` has exactly two values: `SYNTHETIC_GOLDEN_D1` and
`HISTORICAL_DEVELOPMENT_D2`. Immutable `SyntheticSession` and
`HistoricalDevelopmentSession` share the same bar, universe, mask and halt
validation. Historical sessions require stage `development` and a New York
session date from 2016-01-04 through 2021-12-31 inclusive. This check precedes
iteration over supplied bars. Historical later stages are forbidden.

The computational core preserves input provenance in features, intents, simulation
records, summaries and gate reports. Metrics reject mixed/unknown sources.
Bootstrap and gates accept historical provenance only for development. The D1
CLI still supports only `validate` and `conformance`; its synthetic evaluation
wrapper remains synthetic-only. Future D2 needs a separately frozen adapter and
runner, a committed pre-performance manifest, and separate authorization.

Ledger schema 2 binds one provenance and optional fixture marker per ledger.
Provenance participates in trial identity but does not add registered slots:
the frozen schedule still has 53 total slots, 26 for development. Existing schema
1 evidence is preserved and remains readable by its original D1 implementation;
D1R does not migrate or rewrite it. Open a new versioned ledger for D1R.

For historical-mode ledger attempts, the future runner must explicitly supply
`historical_price_rows_exposed`, a nonnegative integer denoting cumulative rows
exposed by that attempt at the event. Finish counts cannot decrease from the
attempt's start. Counts are not summed across events to infer unique rows.
Synthetic events require zero. Generated provenance tests use
`GENERATED_PROVENANCE_FIXTURE` and a matching `exposure_accounting_scope`, so
modeled fixture counts cannot be presented as actual archive access. Unknown
markers or mixed fixture/actual metric records are rejected.

The new tests are in `tests/trading_research/v2_provenance`. They construct all
prices in memory, block network calls and archive reads, and compare identical
generated inputs under both provenance modes. The original 225 D1 conformance
cases remain unchanged in `tests/trading_research/v2`.

Run from the runtime repository:

```powershell
../.venv/Scripts/python.exe -m pytest tests/trading_research tests/trading_runtime -q --basetemp=../.test-tmp/v2d1r-final --tb=short
$scripts = Get-ChildItem scripts/trading_* -File | ForEach-Object { $_.FullName }
../.venv/Scripts/python.exe -m ruff check trading_research tests/trading_research trading_runtime tests/trading_runtime $scripts
```

The private D1R freeze records actual committed module hashes, test evidence,
function-level diff classification and the preserved original D1 identities.
The aborted D2 preflight is `PRE_EXPOSURE_IMPLEMENTATION_BLOCK`, not a trial.
