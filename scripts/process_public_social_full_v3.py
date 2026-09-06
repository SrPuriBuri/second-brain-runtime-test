from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import process_public_social_batch as base
import process_public_social_full_v2 as v2


def _process_unique(url: str) -> dict[str, Any]:
    gemini = None
    gemini_error = None
    if os.getenv("GEMINI_API_KEY"):
        try:
            gemini = v2.Gemini()
        except Exception as exc:
            gemini_error = str(exc)

    kind = base.platform(url)
    if kind == "youtube":
        result = v2.process_youtube(url, gemini)
    elif kind in {"tiktok", "instagram"}:
        result = v2.process_social(url, kind, gemini)
    else:
        result = {
            "platform": kind,
            "status": "failed",
            "warnings": ["Unsupported source type"],
            "runtime_seconds": 0.0,
        }

    if gemini_error:
        result.setdefault("warnings", []).append(f"Gemini initialization failed: {gemini_error}")

    used_gemini = "gemini" in (result.get("sensors") or {})
    if result.get("status") != "complete" or not used_gemini:
        result["needs_local_fallback"] = True
    else:
        result["needs_local_fallback"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("SECOND_BRAIN_MAX_CONCURRENT_SOURCES", "2")),
    )
    args = parser.parse_args()

    payload = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    urls = [str(url) for url in payload["urls"]]

    canonical_by_input = [v2.canonical(url) for url in urls]
    unique_urls: list[str] = []
    for url in canonical_by_input:
        if url not in unique_urls:
            unique_urls.append(url)

    workers = max(1, min(args.workers, len(unique_urls)))
    started = time.monotonic()
    results_by_url: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_unique, url): url for url in unique_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results_by_url[url] = future.result()
            except Exception as exc:
                results_by_url[url] = {
                    "platform": base.platform(url),
                    "status": "failed",
                    "warnings": [f"{exc.__class__.__name__}: {exc}"],
                    "runtime_seconds": 0.0,
                    "needs_local_fallback": True,
                }

    seen: set[str] = set()
    results = []
    for original, canonical in zip(urls, canonical_by_input):
        if canonical in seen:
            results.append({
                "request_url": original,
                "canonical_url": canonical,
                "status": "duplicate",
                "runtime_seconds": 0.0,
                "needs_local_fallback": False,
            })
            continue
        seen.add(canonical)
        result = dict(results_by_url[canonical])
        result["request_url"] = original
        result["canonical_url"] = canonical
        results.append(result)

    final = {
        "schema_version": 3,
        "purpose": "adaptive-public-second-brain-six-source-test",
        "gemini_secret_configured": bool(os.getenv("GEMINI_API_KEY")),
        "input_count": len(urls),
        "unique_source_count": len(unique_urls),
        "max_concurrent_sources": workers,
        "total_runtime_seconds": round(time.monotonic() - started, 3),
        "local_fallback_count": sum(bool(r.get("needs_local_fallback")) for r in results),
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "unique_source_count": final["unique_source_count"],
        "max_concurrent_sources": workers,
        "total_runtime_seconds": final["total_runtime_seconds"],
        "local_fallback_count": final["local_fallback_count"],
        "statuses": [r.get("status") for r in results],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
