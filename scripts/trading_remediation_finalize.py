"""B2 metadata-only assembly from completed local evidence. No provider calls."""

# ruff: noqa: E402 -- local script bootstraps sibling runtime imports
import json
import gzip
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "second-brain-runtime-test"))
from trading_research.archive import atomic_json, file_hash, filesystem_path
from trading_research.dataset_manifest import sha256
from trading_research.data_quality import utc_stamp
from trading_research.restore import read_jsonl
from trading_research.remediation import (
    gap_inventory,
    build_mask,
    normalize_in_kind,
    freeze_view,
    verify_view,
    validate_query,
)
from trading_runtime.market_calendar import NY
from trading_research.dataset_cli import local_safety

DID = "d5c90a9b56b72e2e4a0e96589289bf70ea967f2f9b39c4fea14115315fb330fa"
SID = "aafd7f35583adc7e327d8cc7c9509a7a6c0a3d5063ef28684a43bcbb5e90e800"
P = ROOT / "second-brain/projects/ai-stock-trader"
B1 = P / "research-v2/dataset-v2"
OUT = P / "research-v2/remediation-v2"
DATA = filesystem_path(ROOT / "aist-data")
PARENT = DATA / "ai-stock-trader/datasets" / DID / SID
WORK = DATA / "ai-stock-trader/remediation-v2"


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def put(name, obj):
    atomic_json(OUT / name, obj)


def md(name, text):
    (OUT / name).write_text(text.strip() + "\n", encoding="utf-8")


SOURCES = {
    "nyse_mwcb": {
        "url": "https://www.nyse.com/publicdocs/nyse/markets/nyse/Report_of_the_Market-Wide_Circuit_Breaker_Working_Group.pdf",
        "locator": "March 31 2021 report, pages 4-5; four trigger/reopening times",
    },
    "siac_2019": {
        "url": "https://www.sec.gov/files/rules/sro/nasdaq/2019/34-86842.pdf",
        "locator": "August 30 2019, pages 1-3 and footnote 4; August 12 CQS/CTS gaps, Networks A/B",
    },
    "issuer_2016": {
        "url": "https://www.sec.gov/Archives/edgar/data/1064641/000119312516786454/d235287dncsr.htm",
        "locator": "2016 annual report; ticker table and Note 1, page 94",
    },
    "issuer_2018": {
        "url": "https://www.sec.gov/Archives/edgar/data/1064641/000119312518342039/d623019dncsr.htm",
        "locator": "2018 annual report; fund identity/ticker table, Note 1",
    },
    "xlf_notice": {
        "url": "https://www.sec.gov/Archives/edgar/data/1064641/000119312516699787/d246445d497.htm",
        "locator": "September 2 2016 prospectus supplement; planned September 16 reorganization",
    },
    "box_ex_date": {
        "url": "https://boxoptions.com/assets/BOXOnnMemo203915.pdf",
        "locator": "September 16 2016, page 1, memo 203915/OCC 39575; ex September 19; underlying XLF unchanged",
    },
    "cboe_identity": {
        "url": "https://cdn.cboe.com/resources/regulation/rule_filings/other/options_filings/SR-CBOE-2017-050.pdf",
        "locator": "2017 filing pages 3-4: DIA/SPY/IWM/QQQ security identity table; references 2016 predecessor",
    },
    "qqq_2016": {
        "url": "https://www.sec.gov/Archives/edgar/data/1067839/000119312516517136/d121382d497.htm",
        "locator": "March 24 2016 prospectus supplement; PowerShares QQQ Trust Series 1",
    },
}


def main():
    local_safety(P)
    before = load(WORK / "parent-before.json")
    assert before["result"] == "RESTORE_PASS" and before["snapshot_id"] == SID
    assert file_hash(PARENT / "manifest.json") == before["manifest_sha256"]
    assert file_hash(PARENT / "checksums.json") == before["checksums_sha256"]
    assert (
        file_hash(OUT / "REMEDIATION_PROTOCOL_V2.md")
        == "9eb6484df85c60207804927f4e4cb1397f0650665af1fd87931782a3c4de2d9b"
    )
    coverage = load(B1 / "COVERAGE_AUDIT_V2.json")
    calendar = read_jsonl(PARENT / "calendar/sessions.jsonl.gz")
    original = load(B1 / "UNIVERSE_FREEZE_V2.json")
    inv = gap_inventory(coverage, calendar)
    print("Parent gaps expanded and count-verified", flush=True)
    query_plan = load(OUT / "TARGETED_QUERY_PLAN_V2.json")
    query_dir = WORK / "queries" / sha256(query_plan)
    result = load(query_dir / "result.json")
    receipt_rows = [
        json.loads(x) for x in (query_dir / "receipts.jsonl").read_text().splitlines()
    ]
    attempts = [a for r in receipt_rows for a in r.get("attempts", [])]
    provider = {
        "query_plan_hash": sha256(query_plan),
        "definitions": [],
        "attempts": len(attempts),
        "pages": 0,
        "rows": 0,
        "stored_oos_rows": 0,
        "oos_requests": 0,
        "reconstruction": False,
        "receipts_sha256": file_hash(query_dir / "receipts.jsonl"),
        "files": {},
        "results_sha256": file_hash(query_dir / "result.json"),
    }
    positive = {}
    for outcome in result["outcomes"]:
        q = outcome["query"]
        validate_query(q)
        assert outcome["query_id"] == sha256(q) and q in query_plan["queries"]
        folder = query_dir / outcome["query_id"]
        assert (
            load(folder / "complete.json") == outcome
            and outcome["status"] == "COMPLETE"
        )
        assert len(outcome["pages"]) == 1
        for name, digest in outcome["files"].items():
            assert file_hash(folder / name) == digest
            provider["files"][outcome["query_id"] + "/" + name] = digest
        count = 0
        for page in outcome["pages"]:
            payload = json.loads(gzip.decompress((folder / page["file"]).read_bytes()))
            assert (
                sha256(payload["body"]) == page["content_hash"]
                and payload["query"] == q
            )
            rows = (payload["body"].get(q["route"]) or {}).get(
                q["params"]["symbols"], []
            )
            assert len(rows) == page["rows"]
            for row in rows:
                stamp = utc_stamp(row["t"])
                assert (
                    utc_stamp(q["params"]["start"])
                    <= stamp
                    <= utc_stamp(q["params"]["end"])
                )
                assert stamp.year < 2025
                slot = stamp.replace(
                    minute=(stamp.minute // 5) * 5, second=0, microsecond=0
                ).isoformat()
                key = q["params"]["symbols"] + "/" + slot
                if slot in inv["missing"][q["params"]["symbols"]]:
                    positive[key] = {
                        "classification": "EXPLAINED_PROVIDER_GAP",
                        "evidence": outcome["query_id"],
                        "basis": "Positive native observation inside parent missing slot; no reconstruction",
                    }
            count += len(rows)
        assert count == outcome["rows"]
        provider["rows"] += count
        provider["pages"] += len(outcome["pages"])
        provider["definitions"].append(
            {
                "query_id": outcome["query_id"],
                **q,
                "status": outcome["status"],
                "rows": count,
                "pages": len(outcome["pages"]),
            }
        )
    assert provider["attempts"] == 23 and provider["pages"] == 23
    provider["actual_requests_verified"] = all(
        a.get("http_status") == 200 for a in attempts
    )
    assert provider["actual_requests_verified"]
    provider["retries"] = 0
    provider["positive_missing_slots"] = positive
    put("PROVIDER_REMEDIATION_V2.json", provider)
    halts = []
    for day, start, end in [
        ("2020-03-09", "09:34:13", "09:49:13"),
        ("2020-03-12", "09:35:44", "09:50:44"),
        ("2020-03-16", "09:30:01", "09:45:01"),
        ("2020-03-18", "12:56:17", "13:11:17"),
    ]:
        halts.append(
            {
                "id": "MWCB-" + day,
                "start": day + "T" + start + "-04:00",
                "end": day + "T" + end + "-04:00",
                "verified": True,
                "source_ref": SOURCES["nyse_mwcb"]["url"],
                "end_meaning": "Reopening auctions begin; not proof every individual instrument reopened immediately",
            }
        )
    action = normalize_in_kind(
        {
            "symbol": "XLF",
            "distributed_symbol": "XLRE",
            "ex_date": "2016-09-19",
            "ratio": "0.139146",
            "announcement_document_date": "2016-09-02",
            "declaration_date": "2016-09-16",
            "rebalance_date": "2016-09-16",
            "record_date": "2016-09-21",
            "payment_date": "2016-09-22",
            "source_ref": SOURCES["issuer_2016"]["url"],
            "ex_date_source": SOURCES["box_ex_date"]["url"],
            "announcement_source": SOURCES["xlf_notice"]["url"],
            "underlying_ticker_changed": False,
            "option_symbol_change_not_underlying_change": "XLF options became XLF1; underlying remained XLF",
            "ratio_knowledge_date": "Documented in later 2016 annual report; not backdated to announcement",
            "economic_treatment": "XLF transferred real-estate securities to XLRE for XLRE shares, then distributed XLRE shares to XLF shareholders",
            "status": "PASS_WITH_LIMITATIONS",
        }
    )
    identity = {}
    for symbol, old in sorted(original["members"].items()):
        refs = [
            old["source_ref"],
            SOURCES["issuer_2016" if symbol.startswith("XL") else "cboe_identity"][
                "url"
            ],
        ]
        if symbol.startswith("XL"):
            refs.append(SOURCES["issuer_2018"]["url"])
        if symbol == "QQQ":
            refs.append(SOURCES["qqq_2016"]["url"])
        identity[symbol] = {
            "status": "VERIFIED_WITH_LIMITATIONS",
            "name": old["provider_name"],
            "inception_date": old["issuer_inception_date"],
            "first_retained_session": old["first_valid_session"],
            "exchange": old["exchange"],
            "source_refs": refs,
            "assessment": "Historical primary identity references, issuer inception and continuous local symbol evidence support fixed-instrument identity. No independent daily security-master census or guarantee of exhaustive action history.",
            "material_events": ["2016-09-19 XLF in-kind XLRE distribution"]
            if symbol in ("XLF", "XLRE")
            else [],
            "unresolved_material_identity_issue": False,
            "point_in_time_broad_universe": False,
        }
        if symbol == "XLRE":
            identity[symbol]["commenced_operations"] = "2015-10-08"
    # Official SIAC evidence explains missing A/B observations, never a trading halt.
    provider_slots = dict(positive)
    for symbol, stamps in inv["missing"].items():
        if symbol == "QQQ":
            continue
        for stamp in stamps:
            if utc_stamp(stamp).astimezone(NY).date().isoformat() == "2019-08-12":
                provider_slots[symbol + "/" + stamp] = {
                    "classification": "EXPLAINED_PROVIDER_GAP",
                    "evidence": SOURCES["siac_2019"]["url"],
                    "basis": "Documented CQS/CTS A/B instability and unreliable trade messages; exact loss per venue cannot be fully reconstructed",
                }
    mask = build_mask(inv, calendar, halts, identity, [action], provider_slots)
    print(
        json.dumps({"universe": mask["universe"], "gates": mask["gates"]}), flush=True
    )
    # Verify all non-halt missing observations are excluded; all old integrity defects zero.
    excluded = {(e["symbol"], e["session"]) for e in mask["sessions"]}
    for item in mask["classifications"]:
        if item["classification"] != "MARKET_WIDE_CIRCUIT_BREAKER":
            assert (
                item["symbol"],
                utc_stamp(item["timestamp"]).astimezone(NY).date().isoformat(),
            ) in excluded
    for symbol in mask["universe"]:
        for key in [
            "duplicates",
            "out_of_order",
            "invalid_prices",
            "invalid_volumes",
            "off_grid",
            "ohlc_violations",
        ]:
            assert coverage["symbols"][symbol]["canonical"][key] == 0
    counts = Counter(c["classification"] for c in mask["classifications"])
    corr = Counter(r["count"] for r in inv["correlation"])
    from bisect import bisect_left, bisect_right

    runs = []
    by_symbol = {
        s: [c for c in mask["classifications"] if c["symbol"] == s]
        for s in coverage["symbols"]
    }
    times = {
        s: [utc_stamp(c["timestamp"]) for c in rows] for s, rows in by_symbol.items()
    }
    correlation = {utc_stamp(r["timestamp"]): r["count"] for r in inv["correlation"]}
    for symbol, q in coverage["symbols"].items():
        for gap in q["gaps"]:
            slots = by_symbol[symbol][
                bisect_left(times[symbol], utc_stamp(gap["start"])) : bisect_right(
                    times[symbol], utc_stamp(gap["end"])
                )
            ]
            assert len(slots) == gap["slots"]
            runs.append(
                {
                    "symbol": symbol,
                    **gap,
                    "classifications": dict(
                        Counter(c["classification"] for c in slots)
                    ),
                    "treatment": "NO_FILL; non-halt affected symbol-sessions excluded",
                    "simultaneous_missing_max": max(
                        correlation[utc_stamp(c["timestamp"])] for c in slots
                    ),
                }
            )
    inv.update(
        parent_dataset_id=DID,
        parent_snapshot_id=SID,
        runs=runs,
        correlation_histogram=dict(corr),
        missing_slots=sum(len(v) for v in inv["missing"].values()),
    )
    summary = {
        "missing_slots": inv["missing_slots"],
        "gap_runs": len(runs),
        "material_runs_gt2": sum(r["slots"] > 2 for r in runs),
        "classifications": dict(counts),
        "correlation_histogram": dict(corr),
        "session_exclusions_all_symbols": len(mask["sessions"]),
        "session_exclusions_included_symbols": sum(
            e["symbol"] in mask["universe"] for e in mask["sessions"]
        ),
        "non_trading_slots_per_symbol": len(mask["non_trading_slots"]),
        "unresolved_cause_is_not_valid_data": True,
        "reconstructed_rows": 0,
    }
    put("GAP_INVENTORY_V2.json", inv)
    put("GAP_CLASSIFICATION_V2.json", {"summary": summary, "runs": runs})
    put("MARKET_HALT_EVIDENCE_V2.json", halts)
    put("CORPORATE_ACTION_XLF_XLRE_V2.json", action)
    put("IDENTITY_VERIFICATION_V2.json", identity)
    put("QUALITY_MASK_V2.json", mask)
    put("SOURCE_EVIDENCE_V2.json", SOURCES)
    universe = {
        "symbols": mask["universe"],
        "excluded_symbols": mask["excluded_symbols"],
        "selection_basis": "Frozen non-performance B2 data-quality and identity gates",
        "candidate_universe_hash": original["candidate_universe_hash"],
        "research_universe_hash": sha256(mask["universe"]),
        "parent_universe_hash": load(PARENT / "manifest.json")["universe_hash"],
        "gates": mask["gates"],
        "period_start": "2016-01-04",
        "period_end": "2024-12-31",
        "replacement_symbols": [],
    }
    put("RESEARCH_UNIVERSE_V2.json", universe)
    view_id = freeze_view(
        DATA,
        PARENT,
        file_hash(OUT / "REMEDIATION_PROTOCOL_V2.md"),
        mask,
        [action],
        identity,
    )
    viewdir = DATA / "ai-stock-trader/research-views" / view_id
    verify_view(viewdir, PARENT)
    decision = {
        "decision": "REMEDIATION_READY_WITH_LIMITATIONS",
        "parent_dataset_id": DID,
        "parent_snapshot_id": SID,
        "parent_b1_decision_unchanged": "DATASET_NOT_READY",
        "view_id": view_id,
        "hashes": {
            "protocol_file": file_hash(OUT / "REMEDIATION_PROTOCOL_V2.md"),
            "quality_mask": sha256(mask),
            "actions": sha256([action]),
            "identity": sha256(identity),
            "halt_calendar": sha256(halts),
            "research_universe": sha256(mask["universe"]),
            "view_file": file_hash(viewdir / "view.json"),
            "view_manifest_file": file_hash(viewdir / "manifest.json"),
            "parent_manifest": file_hash(PARENT / "manifest.json"),
            "parent_checksums": file_hash(PARENT / "checksums.json"),
            "targeted_evidence": sha256(provider["files"]),
        },
        "summary": summary,
        "limitations": [
            "Unresolved causal gaps remain explicitly invalid; masking is not observation recovery.",
            "XLRE structurally fails pre-mask quality gate and is excluded; its raw evidence is retained.",
            "QQQ 2016-02-22 and 2018-05-02/03 anomalies persist in bounded queries; no instrument halt proved.",
            "XLF in-kind event resolved, but numerical cross-day adjustment not validated: windows across ex-date invalid.",
            "Historical identity evidence is not an exhaustive point-in-time security master; ordinary action completeness and knowledge vintages remain limited.",
            "Fixed ETF selection and masks reflect known data history. Future OOS legitimacy requires separate preregistration; no blind-universe claim.",
        ],
        "oos_price_requests": 0,
        "stored_oos_rows": 0,
        "strategy_return_calculations": 0,
        "broker_mutations": [0, 0, 0],
        "no_strategy_research_authorized": True,
        "parent_after_verification_required": True,
    }
    assert mask["universe"] and mask["excluded_symbols"] == ["XLRE"]
    put("REMEDIATION_DECISION_V2.json", decision)
    atomic_json(
        WORK / "assembly.json",
        {
            "view_id": view_id,
            "summary": summary,
            "stage": "METADATA_ASSEMBLED_PENDING_FINAL_AUDIT",
        },
    )
    print(json.dumps({"view_id": view_id, "summary": summary}), flush=True)


if __name__ == "__main__":
    main()
