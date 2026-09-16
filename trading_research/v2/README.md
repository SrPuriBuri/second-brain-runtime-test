# V2 D1 synthetic simulator

This package implements the frozen CSLC L15/L30/L45 experiment and clarifications
C1 (numbers), C2 (gates/quantiles), C3 (bar timing), and C4 (SEVERE tail counts).
Only L30 may pass sequential gatekeeping. All historical execution remains disabled.

## Authority and commands

The canonical JSON remains in the private project. The runtime requires the five
independently pinned protocol/clarification hashes and their latest effective hash.
It never obtains expected hashes from a candidate JSON file.

From the public repository:

Use CPython 3.12.14 and trading-v2-requirements.txt for the frozen D1 runtime.
Exact platform, NumPy, pytest and tzdata/NY hashes are recorded in the private
dependency freeze; a different dependency fingerprint requires re-conformance.

```powershell
../.venv/Scripts/python.exe -m trading_research.v2.runner validate --project ../second-brain/projects/ai-stock-trader --data-root ../aist-data
../.venv/Scripts/python.exe -m trading_research.v2.runner conformance --project ../second-brain/projects/ai-stock-trader --data-root ../aist-data --output ../d1-conformance.json
```

An existing output cannot be overwritten. The command accepts no price input path.
Only view/parent/checksum metadata is opened for data identity verification.
`development`, `validation`, `holdout`, `oos`, `trade`, `paper`, and `live` are
rejected. The Phase C guard is unchanged.

Tests need the private metadata directory and the bound local archive **metadata**
at the sibling locations above. `AIST_V2_SPEC_DIR` can override the protocol path.
No raw price object is opened. A checkout without this evidence fails closed;
tests do not silently skip authority checks. All test prices are generated
in Python fixtures, and socket access is blocked by the synthetic test fixture.

## Modules

- `bindings`: pinned Base/C1/C2/C3/C4/effective hash and metadata/safety checks.
- `numeric`, `models`: exact Decimal source prices, explicit 34-digit context,
  binary64 boundary, same-session synthetic inputs and versioned tzdata source.
- `features`, `signal`: synchronous full panel, rank-one selection, fixed filters.
- `fills`, `clock`, `simulator`: independent scenario fills, quantity, protections,
  exact time exits, halt clock, C3 interval evidence and retained indeterminate cases.
- `controls`: independent SPY/delay paths and NO_TRADE; no inherited canonical fills.
- `metrics`: sorted scored outcomes, concentration/removal, Type-7 quantiles,
  explicit single-ledger C4 diagnostics. Tail counts never create a value gate.
- `bootstrap`: fresh seeded PCG64, shared deterministic five-session block indices.
- `gates`: stage-local gates from pinned authority and sequential canonical policy.
- `ledger`: the 53 declared slots, deterministic identities and append-only attempts.
- `conformance`, `runner`: synthetic-only evidence and relevant dependency hashes.

There is no real-data adapter or broker API. Synthetic constructors
are an internal testing interface, not authentication for historical data.
Later execution requires separately authorized code and data-access controls.

## Ledger and reproducibility

The ledger is restricted to synthetic D1 writes. Every trial identity binds all six
spec identities, implementation/config/dependency hashes, parent/child, family,
variant, stage, cost, purpose, delay, record type, subperiod and bootstrap seed.
An identical retry appends another attempt under the same identity. Changed
code/config has another identity and cannot reuse a consumed experiment slot as
fresh evidence. Failed/empty attempts consume the original slot. Each event is
hash chained, written to a temporary file, flushed, and atomically renamed under
an exclusive writer lock. Incomplete temporaries are ignored; missing/tampered
final events fail closed. A stale writer lock requires explicit operator recovery,
not automatic deletion. Completed event files are never overwritten.

Freeze evidence belongs privately and must reference the actual public commit.
After committing, repeat conformance with `--committed`; this hashes Git HEAD blob
bytes and rejects source differences (ordinary Git CRLF/LF checkout conversion is
allowed). Default precommit reports are explicitly UNFROZEN_WORKTREE_BYTES.
The implementation digest hashes the sorted relative-path-to-SHA256 map of the
final committed modules, including the metadata/safety dependencies used here.
Tests and documentation are additionally identified by the public commit.
NumPy, Python, pytest and explicit tzdata/NY bytes are recorded; unrelated
machine packages are excluded.

OHLC stop/target evidence is a half-open execution interval; accounting at bar end
is not a claim about the actual fill instant. Raw bar prices are never filled,
interpolated or repaired. Undefined PF cannot pass a positive-PF gate. Descriptive
Type-7 quantiles and bootstrap inverse empirical CDF quantiles remain distinct.

No D2 development results are produced in D1.
