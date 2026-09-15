# V2 protocol metadata boundary

Phase 3V2-C preregisters future research; it provides no strategy implementation, market-data acquisition or execution command. The canonical protocol and research review belong in the private repository's `projects/ai-stock-trader/research-v2/protocol-v2/` directory.

Use `v2_protocol.load_protocol(path, expected_hash=...)` with the independently pinned digest from the committed preregistration. Do not derive the trusted digest from the candidate file being checked. Duplicate JSON keys, non-finite JSON, unsupported scope, overlapping splits, changes to the frozen mask policy, trial-budget expansion and V1 signal-schema substitutions fail closed.

`verify_bindings(protocol, view, parent_manifest, checksums)` verifies child/parent/universe/quality-mask/halt/action identities using metadata only. It does not read bars or replace the full parent-object restore audit already recorded by B2. Before future authorized execution, both the pinned metadata and every underlying dataset object must still verify. `verify_authorities` checks the four pinned policy/safety files and the required disabled execution state.

The present declarative schema supports one cross-sectional convergence family with a canonical formation window and two neighbors. Recognition of that schema is not proof that a future implementation obeys it: Phase D must add reviewed deterministic implementation and synthetic conformance tests before any historical run. The old V1 simulator is not automatically compatible with V2 panel features, quality masks or halt timing.

`require_command` admits only `validate`. `require_market_request` rejects every market request in Phase C, including all pre-2025 requests; its existing hard date guard also rejects any 2025+ interval. Nothing here authorizes development, validation, holdout, OOS or Paper execution. Any future executor must use a separately authorized entry point and retain all preregistered stage gates and independent hash pins.

No historical performance is calculated by this module. Its tests use synthetic metadata; the existing research tests retain their synthetic simulator fixtures without touching the real archive. No credentials or broker account are required.
