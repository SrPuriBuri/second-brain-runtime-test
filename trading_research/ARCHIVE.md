# Durable historical archive (Phase 3V2-B1)

Data engineering only. The separate `trading_research.dataset_cli` avoids importing
the V1 strategy CLI/engine. The 14 user-proposed ETF symbols are a locked candidate
set, not a verified frozen universe. Actual identity evidence and full coverage
are mandatory before snapshot acceptance. No return-based selection is permitted.

`AIST_DATA_ROOT` selects durable local storage. Default: sibling `aist-data` beside
the two repositories. The CLI prints its resolved path, rejects either repository
and any other git checkout, and uses Windows extended paths internally for long
content hashes. No usernames or machine-specific paths are embedded in code.
No GitHub Actions workflow was added or executed for this phase.

Commands from the public repository:

```
python -m trading_research.dataset_cli dataset-plan
python -m trading_research.dataset_cli dataset-status
python -m trading_research.dataset_cli dataset-download --retention <review.json> --max-chunks 10
python -m trading_research.dataset_cli dataset-resume --retention <review.json> --max-chunks 10
python -m trading_research.dataset_cli dataset-audit --identity <identity.json>
python -m trading_research.dataset_cli dataset-freeze --identity <identity.json>
python -m trading_research.dataset_cli dataset-restore-test --snapshot <frozen-directory>
```

**Current gate: STORAGE_TERMS_UNRESOLVED.** Do not run a bulk archive until a
source-backed review establishes permission for private local retention. A review
record requires status=PERMITTED, provider=alpaca, feed=sip,
private_local_retention=true, source_ref and reviewed_at. These fields record an
actual review; they are not permission obtained by editing a flag. The CLI checks
this before reading credentials or calling the provider. Existing GitHub secrets
are not downloadable credentials for local execution. Once legally resolved,
local execution needs existing Alpaca credentials supplied privately via the
runtime environment; never put credentials in plans, command arguments or git.

The plan binds 2016-01-01–2024-12-31, SIP/5Min/raw/asof=-, objective universe
rationale and acceptance criteria. There are 1,512 symbol-month chunks. Last
request ends 2024-12-31T23:59:59Z; any request crossing into 2025 is rejected before
HTTP and counted separately as blocked. Unexpected OOS response rows are rejected
before storage. No IEX fill, news analysis or strategy calculation exists here.

Native provider records (all original fields, including extended-hours records)
remain in immutable cached response pages. JSON numeric spelling is canonicalized;
this is a provider-observation archive, not an exact HTTP wire-byte capture.
Regular-session canonical OHLCV uses UTC timestamps and decimal strings, interpreted
against the retained exchange calendar in America/New_York. Gzip JSONL has sorted
keys and mtime=0; compressed file hashes bind the actual frozen bytes. Different
compression/runtime versions can change byte hashes without a vendor revision;
compare logical provider payload hashes before attributing a change to the vendor.
Adjusted variants are deliberately not downloaded in the bulk primary plan. The
existing provider adapter can audit separate pre-2025 event windows later; those
retrospective levels must never enter the raw feature layer silently.

Each completed request page and chunk has a content-addressed object and atomic
checkpoint. A failed second page resumes from that page; prior pages are re-read
and hash-verified locally. Requests have one-second pacing and at most three
transient attempts with exponential waits. All attempts receive receipts. OS writer
locks prevent concurrent downloads/freezes and release if the process dies.
Partial directories remain recognizable; never remove completed evidence to resume.

Coverage evaluates the entire planned calendar, preserves defects rather than
sorting them away, and records longest missing sequences. Criteria fixed before
real acquisition: per-symbol completeness >=99.9%, no unexplained sequence over
two slots, no duplicate/order/timestamp/OHLCV/off-grid defects, no unresolved
material action or identity issue. Missingness remains unattributed unless there
is independent evidence; no-trade intervals, halts and dividend gaps are not
automatically vendor errors. Material split-like discontinuities use documented
events; large unmatched gaps block acceptance. Corporate knowledge time is unverified.
Early-close/DST handling derives from actual calendar open/close, not observed counts.

`--identity` requires per-symbol asset_id matched to current active/tradable assets,
etf_verified, identity_continuity_verified and a source_ref. Boolean claims require
real issuer/exchange evidence; current assets alone are insufficient. A later
common start is reported from first observed sessions, but missing early history
still fails the original full-period acceptance. Exclusion or a revised period
requires an explicit new evidence-based plan, never silent substitution.

Final layout:
`<root>/ai-stock-trader/datasets/<dataset-id>/<snapshot-id>/` contains manifest,
checksums, universe, bars, calendar, actions, quality and native provenance. All
writers close and files verify before atomic directory rename. Changed content
creates another snapshot. Restore verifies every checksum, manifest fingerprint,
calendar/actions/universe, all bar timestamps and aggregate identity entirely
offline; tests repeat it in a fresh interpreter with sockets disabled. Local
freezing reports FROZEN_PENDING_FRESH_PROCESS_RESTORE until that separate check.

Private canonical persistence uses metadata-only projections and hashes. No raw
bars, page payloads or large datasets belong in either repository. Do not report a
real RESTORE_PASS or DATASET_FROZEN from synthetic test success. Current real
dataset has zero acquired chunks and no finalized snapshot because of the terms gate.
