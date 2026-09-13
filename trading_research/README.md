# Isolated historical strategy research

The canonical protocol and conclusions are private under
`projects/ai-stock-trader/research/`. This package contains compute only. It never
imports the trading executor, runner, broker client, reconciliation or AI provider.
No account, position, order, live, cancellation or execution method exists here.

## Current result

Harness implemented and covered by synthetic software tests. **No authenticated
historical audit/backtest has run in Phase 3**: automatic approval rejected the
manual historical launch and requires explicit renewed authorization. No strategy
edge, empirical rejection or OOS performance is claimed. Do not confuse a passing
test with a profitable strategy. Existing Phase 1/2 safety tests are unchanged.

## Manual cloud run

`.github/workflows/ai-stock-trader-research.yml` has only `workflow_dispatch`:

* `data-audit`: small fixed historical IEX coverage probes, historical news metadata,
  and Paper calendar. No strategy returns are calculated.
* `run-all`: reads the frozen private protocol/policy, downloads/caches historical
  IEX 5Min raw bars and calendar, audits data, evaluates development and validation,
  writes selection evidence, and opens OOS once **only if a candidate passes**.

Both require separate launch authorization accepted by approval review. The new
workflow shares the existing trading concurrency group, has a 30-minute timeout,
uses current secrets, no paid dependencies, no scheduled trigger and no private
Actions. It does not change strategy, policy, readiness, kill switch or ownership.
The raw dataset/cache exists only on the ephemeral runner, never in public artifacts
or private git. Private derived metrics are sharded, append-only, <=900KB each; all
write messages contain `[skip ci]`. Public logs contain fixed completion/error codes.
The established secret-output guard is reused with lazy import of its default CLI.

`HistoricalSource` accepts only the fixed bars/news/calendar routes, GET, explicit
IEX and raw (or explicitly requested audit adjustment), no redirects. Requests have
40s timeout, 0.4s pacing, <=3 transient attempts and a 2,000-request ceiling. It never
falls back to SIP or changes subscription/credentials. No broker position is read.

## Local reproducibility

Python 3.12 with `trading-dev-requirements.txt`. A real dataset bundle has:
`source=alpaca`, `feed=iex`, `adjustment=raw`, `asof=-`, `timeframe=5Min`,
`retrieved_at`, `calendar=[{date,open,close},...]`, and
`bars={symbol:[{t,o,h,l,c,v},...]}`. Times must be aware; `t` is bar start.
Calendar must come from actual exchange sessions. The cloud runner saves this
bundle to `.research-cache/dataset.json` during its lifetime. Refetching later can
produce a different hash when the provider revises data; provenance detects this,
but ephemeral storage is not a permanent data-vintage archive.

From the public repository, with a genuine local dataset:

```sh
python -m trading_research.cli data-audit --dataset .research-cache/dataset.json
python -m trading_research.cli run-candidate --strategy opening_momentum --dataset .research-cache/dataset.json --protocol ../second-brain/projects/ai-stock-trader/research/protocol-v1.json --policy ../second-brain/projects/ai-stock-trader/POLICY.md --output research-output/manual-opening
python -m trading_research.cli run-all --dataset .research-cache/dataset.json --protocol ../second-brain/projects/ai-stock-trader/research/protocol-v1.json --policy ../second-brain/projects/ai-stock-trader/POLICY.md
python -m trading_research.cli validate --selection research-output/selection.json
python -m trading_research.cli report --selection research-output/selection.json
```

`run-all` freezes selection and leaves local OOS sealed; `validate` displays the
already computed validation record rather than performing an unrecorded rerun.
Manual candidate invocations are additional research trials and retain independent
output/provenance; include them in any research search ledger, never silently tune.
Existing candidate/selection output files are not overwritten. A manual candidate
output cannot authorize OOS; only a passing `run-all` selection can do so.

Only after reviewing an accepted selection:

```sh
python -m trading_research.cli oos --dataset .research-cache/dataset.json --protocol ../second-brain/projects/ai-stock-trader/research/protocol-v1.json --policy ../second-brain/projects/ai-stock-trader/POLICY.md --selection research-output/selection.json
```

Local OOS claims are exclusive files under the canonical project's
`data/evidence/research-v1/oos-claims/`, independent of output directory. Cloud adds
an immutable private Contents API claim keyed by protocol hash. Claims survive
uncertain outcomes; never delete one to retry an OOS or call a contaminated period
untouched. Protocol, data, policy, implementation commit and code hashes must match.

## Event model and limits

Candidate functions receive frozen point-in-time features, not a dataset. Only
completed bars are visible. Session-relative decisions and a full-bar entry delay
avoid same-close fills. Missing signal/history produces NO_TRADE; missing exit data
produces an indeterminate trade and blocks acceptance. Whole-share cash sizing uses
actual assumed fill-to-stop risk within POLICY caps; normalized R uses the initial
signal's planned risk. Portfolio limits and gross realized losses are simulated on
independent cash, with no external account information.

Stop wins ambiguous bars; stop gaps use the worse open; target requires penetration
and gets no favorable gap improvement. Force-flat uses the actual session close−45m,
including early closes. This is a model of protective orders, not proof a scheduler
can manage live exits. OHLC typical-price VWAP is approximate. IEX venue volume is
not consolidated volume. Original news revisions/actions/PIT stocks remain separate
data requirements; current screeners and retrospective AI knowledge are excluded.

## Tests

```sh
python -m pytest tests/trading_runtime tests/trading_research -q
python -m ruff check trading_runtime trading_research tests/trading_runtime tests/trading_research scripts/trading_safe_run.py scripts/trading_research_safe_run.py
```

On restricted Windows desktops, add a fresh workspace `--basetemp` directory if the
default user temporary folder is inaccessible. Tests block all real socket access.
