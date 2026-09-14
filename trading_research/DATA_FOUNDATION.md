# Phase 3V2-A: historical data foundation only

This entry point does not invoke V1 strategy research. Canonical source audits,
sample quality, readiness and the 48-trial V1 ledger are private under
`projects/ai-stock-trader/research-v2/`. V1 protocol and results remain unchanged.

The manual-only `.github/workflows/ai-stock-trader-data.yml` runs
`python scripts/trading_foundation_safe_run.py`. It has no inputs, schedule or
trading command. Fixed representative pre-2025 samples compare IEX/SIP coverage,
corporate actions, assets, calendars and news metadata. Only GET requests reach
Alpaca. Paper assets/calendar URLs are fixed; no orders or positions are read.
The private evidence write is append-only with `[skip ci]`, through the existing
restricted store. It changes no authority or operational readiness.

Interfaces in `providers/base.py` separate market, action, master and news
capabilities. Alpaca's documented capability flags do not promise account access;
probe outcomes are separate evidence. Comprehensive delisted/PIT/revision support
is false until verified. Current assets cannot build a historical universe.
Listing intervals require stable identity and effective plus knowledge times;
a retired ticker alias does not necessarily mean its security was delisted.

`data_quality.py` reports coverage, duplicates, order, OHLCV, timestamps, DST and
early-close slots without calculating returns. Missing slots can reflect a halt,
symbol change or no eligible trades; they are not automatically vendor outages.
The split check compares raw/adjusted factors with explicit event terms. Adjusted
historical prices can embed later actions; these audits do not make adjusted
levels safe for point-in-time features.

`dataset_manifest.py` binds source/feed/symbols/universe/timeframe/dates/adjustment,
retrieval parameters and corporate-action/master versions. Canonical selection
hash, content hash and snapshot hash distinguish selection from vendor revision.
`dataset_cache.py` publishes payload plus manifest by atomic directory rename.
An incomplete `.partial-*` directory is never a completed snapshot. To replay,
call `DatasetCache.read(domain, dataset_id, snapshot_id)` on retained storage.
An identical freeze is reused; changed content gets a different immutable path.

Raw caches are gitignored and are never uploaded by this workflow. Runner storage
is ephemeral; private manifests cannot reconstruct expired bytes. A licensed
durable raw destination and full-period quality/action audit are still needed
before research. Successful samples are not a complete historical archive.
News caches contain metadata, not article full text or hindsight classifications.

Validation (synthetic fixtures, external sockets disabled):

```
python -m pytest tests/trading_research tests/trading_runtime -q
python -m ruff check trading_research tests/trading_research trading_runtime tests/trading_runtime scripts/trading_foundation_safe_run.py
```

No strategy search, OOS performance access or Paper activation is authorized by
this data foundation. Keep strategy tradable=false, kill switch=true and both
execution flags false. Large downloads should only follow a reviewed data-only
plan and explicit retained snapshot provenance.
