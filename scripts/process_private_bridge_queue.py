from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import process_public_social_batch as base
import process_public_social_full_v2 as v2
import process_public_social_full_v3 as v3


PRIVATE_REPO = "SrPuriBuri/second-brain"
RUNTIME_REPO = "SrPuriBuri/second-brain-runtime-test"
SOURCE_TIMEOUT_SECONDS = int(os.getenv("SECOND_BRAIN_SOURCE_TIMEOUT_SECONDS", "120"))
MAX_RETRY_ATTEMPTS = int(os.getenv("SECOND_BRAIN_MAX_RETRY_ATTEMPTS", "4"))


def source_identity(platform: str, canonical_url: str) -> tuple[str, str]:
    parsed = urlparse(canonical_url)
    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]

    stable: str | None = None
    if platform == "youtube":
        stable = v2.youtube_id(canonical_url)
    elif platform == "instagram":
        if len(parts) >= 2 and parts[0] in {"p", "reel", "reels", "tv"}:
            stable = parts[1]
    elif platform == "tiktok":
        for marker in ("photo", "video"):
            if marker in parts:
                index = parts.index(marker)
                if index + 1 < len(parts):
                    stable = parts[index + 1]
                    break

    if stable:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stable)
        return f"{platform}:{stable}", f"runtime_{platform}_{safe}"

    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]
    return f"{platform}:url:{digest}", f"runtime_{platform}_{digest}"


def load_requests(request_dir: Path, limit: int) -> list[tuple[Path, dict[str, Any]]]:
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(request_dir.glob("*.json")):
        if len(loaded) >= limit:
            break
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        url = value.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        retry = value.get("runtime_retry") if isinstance(value.get("runtime_retry"), dict) else {}
        if retry.get("state") == "quarantined":
            continue
        retry_after = retry.get("retry_after")
        if isinstance(retry_after, str) and retry_after:
            try:
                retry_at = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
            except ValueError:
                retry_at = None
            if retry_at is not None and retry_at > datetime.now(timezone.utc):
                continue
        loaded.append((path, value))
    return loaded


def normalized_source(
    source_id: str,
    platform: str,
    canonical_url: str,
    raw: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    description = source.get("description") or source.get("caption")
    title = source.get("title")
    if not description and platform in {"instagram", "tiktok"}:
        description = title

    return {
        "id": source_id,
        "source_type": platform,
        "provider": source.get("provider") or "public-runtime-adaptive",
        "canonical_url": canonical_url,
        "title": title,
        "author": source.get("author") or source.get("uploader") or source.get("channel"),
        "published_at": source.get("published_at") or source.get("upload_date"),
        "description": description,
        "segments": [],
        "metadata": {
            "runtime_repo": RUNTIME_REPO,
            "request_schema_version": raw.get("schema_version"),
            "sensor_policy": raw.get("sensor_policy") or "adaptive",
            "visual_sensitive": bool(result.get("visual_sensitive")),
            "visual_focus_triggers": result.get("visual_focus_triggers") or [],
        },
    }


def scrub_photo_mode_background_audio(
    platform: str,
    assets: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    image_count = sum(1 for asset in assets if asset.get("media_type") == "image")
    video_count = sum(1 for asset in assets if asset.get("media_type") == "video")
    audio_count = sum(1 for asset in assets if asset.get("media_type") == "audio")
    ignored = platform == "tiktok" and image_count > 0 and video_count == 0 and audio_count > 0
    if not ignored:
        return evidence, False

    cleaned = deepcopy(evidence)
    cleaned["transcript"] = {
        "available": False,
        "language": None,
        "text": "",
        "segments": [],
        "provenance": "background audio intentionally ignored for TikTok Photo Mode",
    }
    cleaned.pop("secondary_transcript", None)
    return cleaned, True


def quality_gate(result: dict[str, Any], evidence: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if result.get("status") != "complete":
        reasons.append("runtime status is not complete")

    platform = str(result.get("platform") or "")
    assets = ((result.get("assets") or {}).get("items") or []) if isinstance(result.get("assets"), dict) else []
    image_count = sum(1 for item in assets if item.get("media_type") == "image")
    video_count = sum(1 for item in assets if item.get("media_type") == "video")

    transcript = evidence.get("transcript") if isinstance(evidence.get("transcript"), dict) else {}
    transcript_chars = len(str(transcript.get("text") or "").strip())
    visual = evidence.get("visual_evidence") if isinstance(evidence.get("visual_evidence"), list) else []

    if platform == "youtube":
        if transcript_chars < 500:
            reasons.append("YouTube transcript is too small")
        if result.get("visual_sensitive") and len(visual) < 3:
            reasons.append("visual-sensitive YouTube lacks timestamped visual evidence")
    elif image_count > 0 and video_count == 0:
        minimum = max(1, int(image_count * 0.8))
        if len(visual) < minimum:
            reasons.append(f"image post visual coverage {len(visual)}/{image_count} is below gate")
    elif video_count > 0:
        if transcript_chars < 40 and len(visual) < 1:
            reasons.append("video has neither useful transcript nor visual evidence")
    else:
        if transcript_chars < 40 and len(visual) < 1:
            reasons.append("source has insufficient durable evidence")

    return not reasons, reasons


def build_evidence_record(
    request_entries: list[tuple[Path, dict[str, Any]]],
    canonical_url: str,
    result: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    platform = str(result.get("platform") or base.platform(canonical_url))
    source_id, output_dir = source_identity(platform, canonical_url)

    assets = ((result.get("assets") or {}).get("items") or []) if isinstance(result.get("assets"), dict) else []
    evidence = deepcopy(result.get("evidence") if isinstance(result.get("evidence"), dict) else {})
    evidence, background_audio_ignored = scrub_photo_mode_background_audio(platform, assets, evidence)

    passed, gate_reasons = quality_gate(result, evidence)
    if not passed:
        raise RuntimeError("; ".join(gate_reasons))

    requests_payload = []
    for path, raw in request_entries:
        requests_payload.append({
            "request_file_sha256": hashlib.sha256(path.name.encode("utf-8")).hexdigest(),
            "requested_by": raw.get("requested_by"),
            "purpose": raw.get("purpose"),
            "needs_visual_evidence": raw.get("needs_visual_evidence"),
            "visual_evidence_required": raw.get("visual_evidence_required"),
            "user_state_rule": raw.get("user_state_rule"),
        })

    first_raw = request_entries[0][1]
    warnings = list(result.get("warnings") or [])
    if background_audio_ignored:
        warnings.append("TikTok Photo Mode background audio was intentionally excluded from semantic evidence.")

    record = {
        "schema_version": 3,
        "status": "ready_for_chatgpt",
        "source_id": source_id,
        "request_url": first_raw.get("url"),
        "canonical_url": canonical_url,
        "requested_by": first_raw.get("requested_by") or "second-brain-public-runtime",
        "purpose": first_raw.get("purpose") or "second-brain-ingestion",
        "acquisition": {
            "provider": "public-runtime-adaptive",
            "public_runner": True,
            "public_only": True,
            "authenticated": False,
            "raw_media_persisted": False,
            "attempts": result.get("acquisition_attempts") or [],
        },
        "source": normalized_source(source_id, platform, canonical_url, first_raw, result),
        "assets": assets,
        "evidence": evidence,
        "sensors": result.get("sensors") or {},
        "quality_gate": {
            "passed": True,
            "background_audio_ignored": background_audio_ignored,
        },
        "requests": requests_payload,
        "warnings": warnings,
        "runtime": {
            "repository": RUNTIME_REPO,
            "runtime_seconds": result.get("runtime_seconds"),
            "needs_local_fallback": bool(result.get("needs_local_fallback")),
        },
    }
    return record, source_id, output_dir


def mark_retry(entries: list[tuple[Path, dict[str, Any]]], reason: str) -> None:
    now = datetime.now(timezone.utc)
    for path, raw in entries:
        retry = raw.get("runtime_retry") if isinstance(raw.get("runtime_retry"), dict) else {}
        attempts = int(retry.get("attempts") or 0) + 1
        if attempts >= MAX_RETRY_ATTEMPTS:
            raw["runtime_retry"] = {
                "attempts": attempts,
                "state": "quarantined",
                "last_failure_at": now.isoformat(),
                "retry_after": None,
                "reason": reason,
            }
        else:
            delay_hours = min(24, 2 ** attempts)
            raw["runtime_retry"] = {
                "attempts": attempts,
                "state": "backoff",
                "last_failure_at": now.isoformat(),
                "retry_after": (now + timedelta(hours=delay_hours)).isoformat(),
                "reason": reason,
            }
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_canonical(canonical_url: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="second-brain-source-worker-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "input.json"
        output_path = root / "output.json"
        input_path.write_text(json.dumps({"url": canonical_url}), encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker-input",
                    str(input_path),
                    "--worker-output",
                    str(output_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SOURCE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "platform": base.platform(canonical_url),
                "warnings": ["source processing exceeded bounded runtime"],
                "runtime_seconds": SOURCE_TIMEOUT_SECONDS,
                "needs_local_fallback": True,
            }
        if completed.returncode != 0 or not output_path.is_file():
            return {
                "status": "failed",
                "platform": base.platform(canonical_url),
                "warnings": ["isolated source worker failed"],
                "runtime_seconds": 0.0,
                "needs_local_fallback": True,
            }
        return json.loads(output_path.read_text(encoding="utf-8"))


def run_worker(input_path: Path, output_path: Path) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    result = v3._process_unique(str(payload["url"]))
    output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-root")
    parser.add_argument("--limit", type=int, default=int(os.getenv("SECOND_BRAIN_MAX_REQUESTS_PER_RUN", "25")))
    parser.add_argument("--workers", type=int, default=int(os.getenv("SECOND_BRAIN_MAX_CONCURRENT_SOURCES", "2")))
    parser.add_argument("--worker-input")
    parser.add_argument("--worker-output")
    args = parser.parse_args()

    if args.worker_input and args.worker_output:
        return run_worker(Path(args.worker_input), Path(args.worker_output))
    if not args.private_root:
        parser.error("--private-root is required outside worker mode")

    private_root = Path(args.private_root).resolve()
    request_dir = private_root / "bridge_requests"
    evidence_root = private_root / "bridge_evidence"
    request_dir.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)

    loaded = load_requests(request_dir, max(1, args.limit))
    grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path, raw in loaded:
        canonical_url = v2.canonical(str(raw["url"]))
        grouped.setdefault(canonical_url, []).append((path, raw))

    if not grouped:
        print(json.dumps({"processed": 0, "succeeded": 0, "remaining": 0}))
        return 0

    workers = max(1, min(args.workers, len(grouped)))
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(process_canonical, canonical): canonical for canonical in grouped}
        for future in as_completed(future_map):
            canonical = future_map[future]
            try:
                results[canonical] = future.result()
            except Exception as exc:
                results[canonical] = {
                    "status": "failed",
                    "platform": base.platform(canonical),
                    "warnings": [f"{exc.__class__.__name__}: {exc}"],
                    "runtime_seconds": 0.0,
                    "needs_local_fallback": True,
                }

    succeeded = 0
    failed = 0
    for canonical, entries in grouped.items():
        result = results[canonical]
        try:
            record, _, output_dir = build_evidence_record(entries, canonical, result)
        except Exception as exc:
            failed += 1
            warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
            detail = "; ".join(str(item) for item in warnings if item) or str(exc)
            mark_retry(entries, detail[-1500:])
            continue

        destination = evidence_root / output_dir / "evidence.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for path, _ in entries:
            path.unlink(missing_ok=True)
        succeeded += len(entries)

    remaining = len(list(request_dir.glob("*.json")))
    print(json.dumps({
        "processed_request_files": len(loaded),
        "unique_sources": len(grouped),
        "succeeded_request_files": succeeded,
        "failed_unique_sources": failed,
        "remaining_request_files": remaining,
        "wall_seconds": round(time.monotonic() - started, 3),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
