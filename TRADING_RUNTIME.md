# AI Stock Trader — Paper research runtime

Phase 1 is research-only. All production broker mutation methods raise `PHASE1_EXECUTION_DISABLED`; no environment variable unlocks them. The in-memory fake broker exercises the future lifecycle in tests. Private strategy is version 0, RESEARCH_REQUIRED, tradable=false, and the kill switch stays enabled.

## Why two repositories

`SrPuriBuri/second-brain/projects/ai-stock-trader/` holds canonical policy, strategy, schedule, state, ownership ledger and durable evidence. `SrPuriBuri/second-brain-runtime-test/trading_runtime/` holds compute. Private Actions quota is exhausted; only the public workflow runs trading research. No database, private trading workflow or third repository is involved. The social/video workflow and modules are unchanged.

Evidence → provider (no_ai or Gemini) → Pydantic proposal → deterministic policy/risk gate → Paper executor → Paper broker → reconciliation → private journal/projection/handoff and optional notification. The provider never receives a broker/store reference or secrets. The executor re-reads authority, recomputes risk and verifies broker state independently. Production has no registered frozen strategy implementation, and its adapter implements no order mutations.

## Local setup

Python 3.12 is the tested version. From this public repository:

```sh
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install -r trading-dev-requirements.txt
python -m pytest tests/trading_runtime -q
python -m trading_runtime.cli dry-run --fake
```

The offline dry-run uses mock GitHub transport and an in-memory Alpaca account. It does not read environment credentials, call a network service or write canonical files. Simulation constants are synthetic test fixtures, never operational policy. Run-time policy is read exclusively from the private POLICY.md JSON block.

Required for read-only Paper connectivity: `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_SECRET_KEY`. The SDK is constructed with `paper=True`, validates the resolved Paper base URL and verifies active account identity before subsequent calls. `ALPACA_ENDPOINT`, live keys, `PAPER=false` and `LIVE=true` are refused. No live implementation exists.

Required for private evidence: `SECOND_BRAIN_PAT`, a fine-grained GitHub token restricted to `SrPuriBuri/second-brain` with Contents read/write. GitHub tokens cannot be path-scoped; PrivateRepoStore enforces the project path and a narrower write allowlist. Every write includes `[skip ci]`. Do not use an Actions token for private access. The existing social workflow uses a differently named secret; this isolated workflow expects `SECOND_BRAIN_PAT` explicitly.

Optional AI: `SECOND_BRAIN_TRADING_AI_PROVIDER=no_ai` (default) or `gemini`; Gemini also needs existing `GEMINI_API_KEY` and explicit `SECOND_BRAIN_TRADING_MODEL`. It reuses the installed google-genai version without importing video processing code. Gemini gets research candidates, never authority to place orders. Do not infer strategy rules from its output.

DO NOT paste Alpaca keys into ChatGPT, Codex prompts, git files or logs. Set credentials through a secure environment/session or GitHub Actions Secrets UI. The runtime does not load dotenv files. `.env*` files are ignored as defense in depth.

## Commands and evidence

Phase 2 adds `python -m trading_runtime.cli validate-paper`. It performs read-only
Paper authentication, account and asset inventory, ownership assessment, calendar
and slot status, direct IEX/SIP entitlement probes, news/screeners and offline
dry-run. `inventory` uses the same complete diagnostic. It persists a sanitized
report under private `data/evidence/connectivity_<timestamp>_<id>.json`, then merges
observed booleans into `state/readiness.json`; `execution_ready` stays false. It
does not bind the account, edit strategy/kill switch or send notifications.

The public workflow maps the existing secrets `Alpaca_API_KEY` and
`Alpaca_Secret_KEY` into `ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_SECRET_KEY`.
The original Paper-specific secret names remain fallbacks. For private persistence,
`SECOND_BRAIN_PAT` falls back to the existing `SECOND_BRAIN_PRIVATE_REPO_TOKEN`
used by the social workflow. Python still reads only the Paper-specific broker
environment names; the secret aliases cannot choose another endpoint.

Publication and dispatch require authorization. Phase 2 has only a manual trigger;
there is no scheduled trigger. Before broker access and again after the reads and
private persistence, validation requires kill switch enabled, strategy not tradable,
and an explicit `execution_ready: false`. `scripts/trading_safe_run.py`
captures output in memory and checks it against configured secret values before
anything is printed to the job log. It emits a fixed failure code instead of
leaking a matching credential.

An unbound empty account is SAFE_WITH_LIMITATIONS. Unowned exposure or unresolved
ownership is UNSAFE for shared-strategy activation. A Cobre client-ID prefix is
only an inference; otherwise the connection to Cobre Alpha remains unknown.
Boolean capability fields are true only when verified. False can mean unavailable
or not verified: inspect each probe's status and HTTP code. Empty weekend data
and quote freshness are separate from endpoint accessibility. No entitlement probe
buys data or silently substitutes IEX for SIP.

```sh
python -m trading_runtime.cli connectivity
python -m trading_runtime.cli inventory
python -m trading_runtime.cli slot-status
python -m trading_runtime.cli research
python -m trading_runtime.cli dry-run
python -m trading_runtime.cli reconcile
python -m trading_runtime.cli run-slot --slot PREP
python -m trading_runtime.cli run-slot --slot FIRST_SCAN
python -m trading_runtime.cli run-slot --slot LAST_NEW_TRADE
python -m trading_runtime.cli run-slot --slot MANAGE
python -m trading_runtime.cli run-slot --slot FORCE_FLAT
python -m trading_runtime.cli run-slot --slot RECONCILE
python -m trading_runtime.cli report
python -m trading_runtime.cli scheduled
```

The four `scripts/trading_*.py` files wrap connectivity, inventory, dry-run and reconciliation. A requested slot outside its real-calendar window is a successful no-op. There is no force-time override. Manual research and reconciliation generate separate audit runs and cannot submit orders.

Inventory records account verification/status, balances, shared-account nature, tested IEX access, unknown SIP entitlement, tradable count, sample symbols, fractionable count, exchange distribution and clock/calendar. Full inventory prints locally and persists privately. In public Actions it prints only completion status. Other public summaries contain run ID, counts, status and NY/Madrid times; private balances, holdings, strategy text, proposals and news never enter public job summaries or artifacts.

Inspect the private `state/current-state.json`, follow its `handoff` path, then `data/progress/`, `data/journal/`, `data/watchlists/` and `data/evidence/`. Unbound account is an expected bootstrap reconciliation alert. It blocks execution but does not prevent research. Runtime does not bind an account automatically.

## Scheduling and recovery

Alpaca calendar/clock supply market dates and early-close hours. PREP is open−45m; FIRST_SCAN open+60m; LAST_NEW_TRADE open+150m; MANAGE close−120m; FORCE_FLAT close−45m; RECONCILE close−15m; POST_CLOSE_REPORT close+15m. Morning research is 08:10 Europe/Madrid and informational only. Times render in America/New_York and Europe/Madrid, including weeks when DST transitions differ.

Cron wakes every 15 minutes in a broad weekday UTC window. Runtime due windows are 20 minutes, and entries stop at FORCE_FLAT even on early-close days. Workflow concurrency covers all commands. Durable atomic claims are per date/slot, independent of strategy version; the human run ID includes strategy version. Ordinary no-ops neither notify nor write private state. A repeated slot is never replayed automatically.

A crash leaves the claim and pre-submission intent. Reconcile exact `aist-` client IDs and broker bracket children before taking any recovery action. A missing acknowledgment is not proof of failed submission. Unknown intent or identity mismatch blocks execution. Do not delete an intent or rerun under a different ID to bypass a failed run. Read-only manual reconcile/report can recover visibility. A later operator-reviewed recovery procedure must reconcile and deliberately release/replace a claim; no automatic lock expiry is provided.

Private projection writes use expected SHA. Bounded retries rerun the reconcile/transform on fresh content; ownership reservations reject incompatible changes. Completion is persisted before notifying, so an optional messaging failure cannot replay an order. If private storage fails, stop and inspect the workflow failure; durable persistence cannot be promised during a GitHub outage.

## Ownership, exits and activation

Cobre Alpha may share the account. Account cash/equity/buying power are shared, but foreign positions and orders are never managed. The ledger must bind an explicitly validated Paper account ID and match immutable pre-order evidence. Exact broker orders and bracket leg fills establish remaining project quantity. Unknown namespaced orders, net quantity mismatch or foreign orders on the same symbol raise high-severity alerts. No blanket cancel or close-all API exists.

The simulated force-flat lifecycle reconciles, cancels only verified project orders, rechecks cancellation/ownership, persists an exit intent, closes only verified project quantity and verifies flatness. Kill switch does not block these risk-reducing simulated exits. Production cancellation and closing remain disabled in Phase 1. Shared accounts cannot rule out same-symbol races by inspection alone; use a separate Paper account if safe coordination cannot be demonstrated. Do not weaken ownership rules or change Cobre Alpha.

Kill switch starts enabled. Only the user may explicitly authorize a later manual release in the private repo, with `[skip ci]`; runtime cannot edit it. To enable Paper later: research and freeze exact rules, implement a deterministic versioned strategy qualifier, verify dedicated-account/broker ownership and bracket/partial-fill recovery, build a reliable exit watchdog, review the production mutation implementation, test read-only connectivity, then obtain explicit user authorization. Merely editing tradable or an environment variable cannot enable Phase 1 orders. Live trading is never a next mode.

Disable automation via the public Actions workflow's Disable workflow action and enable/retain the private kill switch. This implementation has not been pushed, so its new schedule is not deployed. Do not trigger private Actions. To verify a future submitted Paper order, select Paper in Alpaca, inspect Orders for the journal's exact `aist-` client ID, compare broker order/leg IDs, fills and quantity with private evidence, then run read-only reconciliation. Never submit an extra order as a connectivity test.

## Notifications and limitations

Console/job summary always works. Telegram activates only with both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Discord uses `DISCORD_WEBHOOK_URL`. Provider failures are recorded privately and never retried by rerunning the trading slot. No provider is required for useful research. Tokens and webhook URLs are never printed.

Basic/IEX is the default. SIP 401/403 requests fall back to IEX with provenance; IEX data is never described as consolidated SIP. The shortlist filters at most 50 configured seeds from active/tradable US assets and returns up to the configured 5–15 target; insufficient eligible candidates remain fewer. Liquidity is previous-daily feed-specific dollar volume, not consolidated ADV. News is bounded and optional. Movers/most-active discovery is not needed in v0. Leveraged/inverse detection combines explicit exclusions and asset names; it is not a complete fund taxonomy and needs review before activation.

Production execution, account binding, notifications and real Paper connectivity remain unvalidated. Whole-share sizing is intentional for the future limit-bracket lifecycle. Open/pending risk is conservative and does not assume profitable exits replenish loss budget; it is an estimate, not a guaranteed loss ceiling. Corporate actions and complex shared-account history need additional validation. GitHub scheduled workflows are best-effort and cannot guarantee no overnight exposure; a reliable exit watchdog is a prerequisite for enabling Paper execution.

Alpaca references checked during implementation: [calendar including early closes](https://alpaca.markets/sdks/python/api_reference/trading/calendar.html), [orders, brackets and client IDs](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [market-data feeds](https://docs.alpaca.markets/us/docs/market-data-faq).
