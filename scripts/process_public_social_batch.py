from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


ANALYZER = "mcp-video-analyzer@0.10.0"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}


def run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def resolve_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host not in {"vm.tiktok.com", "vt.tiktok.com"}:
        return url
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SecondBrainRuntimeTest/1.0)"},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return str(response.url)
    except Exception:
        return url


def platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" in host or host == "youtu.be":
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    return "web"


def json_records(value: Any, output: list[list[Any]]) -> None:
    if isinstance(value, list):
        if len(value) >= 3 and isinstance(value[-1], dict):
            output.append(value)
            return
        for item in value:
            json_records(item, output)


def gallery_probe(url: str) -> dict[str, Any]:
    completed = run(
        [
            "python",
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--no-input",
            "--no-colors",
            "--resolve-json",
            "--simulate",
            url,
        ],
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "gallery-dl probe failed")[-1500:])
    payload = json.loads(completed.stdout)
    records: list[list[Any]] = []
    json_records(payload, records)
    metadata = [row[-1] for row in records if isinstance(row[-1], dict)]
    if not metadata:
        raise RuntimeError("gallery-dl returned no metadata records")
    first = metadata[0]

    def render_text(value: Any) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict):
            for key in ("uniqueId", "username", "nickname", "name", "uploader", "channel"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return None

    def first_text(*keys: str) -> str | None:
        for row in metadata:
            for key in keys:
                rendered = render_text(row.get(key))
                if rendered:
                    return rendered
        return None

    return {
        "title": first_text("title"),
        "author": first_text("username", "author", "uploader", "creator"),
        "caption": first_text("description", "content", "caption", "text"),
        "post_id": first_text("post_shortcode", "post_id", "id"),
        "record_count": len(records),
        "category": first.get("category"),
        "subcategory": first.get("subcategory"),
    }


def gallery_download(url: str, output_dir: Path, *, timeout: int = 240) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    completed = run(
        [
            "python",
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--no-input",
            "--no-colors",
            "--directory",
            str(output_dir),
            "--filename",
            "{num:03}_{id}.{extension}",
            url,
        ],
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "gallery-dl download failed")[-1500:])
    return sorted(path for path in output_dir.rglob("*") if path.is_file())


def ytdlp_metadata(url: str) -> dict[str, Any]:
    completed = run(
        ["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist", "--no-warnings", url],
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "yt-dlp metadata failed")[-1500:])
    info = json.loads(completed.stdout)
    keep = {}
    for key in (
        "id", "title", "description", "uploader", "channel", "upload_date",
        "duration", "webpage_url", "view_count", "like_count", "comment_count",
        "availability", "tags", "categories",
    ):
        value = info.get(key)
        if value not in (None, "", [], {}):
            keep[key] = value
    keep["_has_subtitles"] = bool(info.get("subtitles"))
    keep["_has_auto_captions"] = bool(info.get("automatic_captions"))
    return keep


_TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?\.\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?\.\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def clean_caption_text(value: str) -> str:
    value = html.unescape(value)
    value = _TAG_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_vtt(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    segments = []
    i = 0
    while i < len(lines):
        match = _TIMESTAMP_RE.search(lines[i])
        if not match:
            i += 1
            continue
        start = match.group("start")
        end = match.group("end")
        i += 1
        parts = []
        while i < len(lines) and lines[i].strip():
            parts.append(lines[i])
            i += 1
        text = clean_caption_text(" ".join(parts))
        if text:
            segments.append({"start": start, "end": end, "text": text})
    deduped = []
    previous = None
    for segment in segments:
        if segment["text"] == previous:
            continue
        deduped.append(segment)
        previous = segment["text"]
    return {
        "available": bool(deduped),
        "segments": deduped,
        "text": " ".join(item["text"] for item in deduped),
        "source_file": path.name,
    }


def youtube_transcript(url: str, temp_root: Path) -> dict[str, Any]:
    out = temp_root / "yt"
    completed = run(
        [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "es.*,en.*",
            "--sub-format",
            "vtt",
            "--no-playlist",
            "--no-warnings",
            "-o",
            str(out),
            url,
        ],
        timeout=180,
    )
    vtts = sorted(temp_root.glob("yt*.vtt"))
    if not vtts:
        return {
            "available": False,
            "segments": [],
            "text": "",
            "warning": (completed.stderr or completed.stdout or "No VTT captions produced")[-1000:],
        }
    preferred = sorted(
        vtts,
        key=lambda p: (
            0 if ".es" in p.name else 1 if ".en" in p.name else 2,
            len(p.name),
        ),
    )[0]
    return parse_vtt(preferred)


def ytdlp_download(url: str, output_dir: Path, *, timeout: int = 300) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "media.%(ext)s"
    completed = run(
        [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "-o",
            str(target),
            url,
        ],
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "yt-dlp download failed")[-1500:])
    return sorted(path for path in output_dir.iterdir() if path.is_file())


def ocr_image(path: Path) -> str | None:
    completed = run(["tesseract", str(path), "stdout", "--psm", "6"], timeout=45)
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def analyze_video(path: Path, analysis_dir: Path) -> dict[str, Any]:
    analysis_dir.mkdir(parents=True, exist_ok=True)
    completed = run(
        [
            "npx",
            "-y",
            ANALYZER,
            "analyze",
            str(path.resolve()),
            "--detail",
            "standard",
            "--model",
            "base",
            "--out",
            str(analysis_dir.resolve()),
        ],
        timeout=900,
    )
    if completed.returncode != 0:
        return {
            "available": False,
            "error": (completed.stderr or completed.stdout or "video analyzer failed")[-2500:],
        }
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        return {"available": False, "error": "video analyzer returned non-object JSON"}
    # Never persist raw/keyframe files from the test. Keep only textual/structured observations.
    frames = payload.get("frames") if isinstance(payload.get("frames"), list) else []
    compact_frames = []
    for frame in frames:
        if isinstance(frame, dict):
            compact_frames.append({
                "time": frame.get("time"),
                "timestamp": frame.get("timestamp"),
            })
    return {
        "available": True,
        "metadata": payload.get("metadata"),
        "transcript": payload.get("transcript"),
        "ocrResults": payload.get("ocrResults"),
        "timeline": payload.get("timeline"),
        "chapters": payload.get("chapters"),
        "warnings": payload.get("warnings"),
        "frames": compact_frames,
    }


def process_one(url: str) -> dict[str, Any]:
    started = time.monotonic()
    resolved = resolve_url(url)
    kind = platform(resolved)
    result: dict[str, Any] = {
        "request_url": url,
        "resolved_url": resolved,
        "platform": kind,
        "status": "partial",
        "source": {},
        "evidence": {},
        "acquisition_attempts": [],
        "warnings": [],
    }

    with tempfile.TemporaryDirectory(prefix="second-brain-public-test-") as temp:
        root = Path(temp)

        if kind == "youtube":
            try:
                t0 = time.monotonic()
                result["source"] = ytdlp_metadata(resolved)
                result["acquisition_attempts"].append({
                    "method": "yt-dlp-metadata",
                    "success": True,
                    "seconds": round(time.monotonic() - t0, 3),
                })
            except Exception as exc:
                result["acquisition_attempts"].append({"method": "yt-dlp-metadata", "success": False, "error": str(exc)})
                result["warnings"].append(str(exc))

            try:
                t0 = time.monotonic()
                transcript = youtube_transcript(resolved, root)
                result["evidence"]["transcript"] = transcript
                result["acquisition_attempts"].append({
                    "method": "yt-dlp-captions",
                    "success": bool(transcript.get("available")),
                    "seconds": round(time.monotonic() - t0, 3),
                })
            except Exception as exc:
                result["warnings"].append(f"caption acquisition: {exc}")

            if not result.get("evidence", {}).get("transcript", {}).get("available"):
                try:
                    t0 = time.monotonic()
                    media = ytdlp_download(resolved, root / "youtube-media")
                    video = next((p for p in media if p.suffix.lower() in VIDEO_EXTS), None)
                    if video:
                        result["evidence"]["video_analysis"] = analyze_video(video, root / "analysis")
                    result["acquisition_attempts"].append({
                        "method": "yt-dlp-video+local-analysis",
                        "success": video is not None,
                        "seconds": round(time.monotonic() - t0, 3),
                    })
                except Exception as exc:
                    result["warnings"].append(f"youtube media fallback: {exc}")

        elif kind in {"tiktok", "instagram"}:
            try:
                t0 = time.monotonic()
                result["source"] = gallery_probe(resolved)
                result["acquisition_attempts"].append({
                    "method": "gallery-dl-probe",
                    "success": True,
                    "seconds": round(time.monotonic() - t0, 3),
                })
            except Exception as exc:
                result["acquisition_attempts"].append({"method": "gallery-dl-probe", "success": False, "error": str(exc)})
                result["warnings"].append(f"gallery probe: {exc}")

            files: list[Path] = []
            try:
                t0 = time.monotonic()
                files = gallery_download(resolved, root / "gallery")
                result["acquisition_attempts"].append({
                    "method": "gallery-dl-download",
                    "success": bool(files),
                    "seconds": round(time.monotonic() - t0, 3),
                })
            except Exception as gallery_exc:
                result["warnings"].append(f"gallery download: {gallery_exc}")
                try:
                    t0 = time.monotonic()
                    files = ytdlp_download(resolved, root / "ytdlp")
                    result["acquisition_attempts"].append({
                        "method": "yt-dlp-download-fallback",
                        "success": bool(files),
                        "seconds": round(time.monotonic() - t0, 3),
                    })
                except Exception as ytdlp_exc:
                    result["warnings"].append(f"yt-dlp fallback: {ytdlp_exc}")

            images = [p for p in files if p.suffix.lower() in IMAGE_EXTS]
            videos = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
            audios = [p for p in files if p.suffix.lower() in AUDIO_EXTS]
            result["evidence"]["asset_summary"] = {
                "images": len(images),
                "videos": len(videos),
                "audio": len(audios),
                "raw_media_persisted": False,
            }
            if images:
                result["evidence"]["images"] = [
                    {
                        "position": index,
                        "ocr_text": ocr_image(path),
                        "size_bytes": path.stat().st_size,
                    }
                    for index, path in enumerate(images, start=1)
                ]
            if videos:
                result["evidence"]["video_analysis"] = analyze_video(videos[0], root / "analysis")

        meaningful = bool(result.get("source")) or bool(result.get("evidence"))
        result["status"] = "ready_for_chatgpt" if meaningful else "failed"

    result["runtime_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list) or not urls:
        raise SystemExit("request JSON must contain a non-empty urls array")

    started = time.monotonic()
    results = []
    for index, url in enumerate(urls, start=1):
        print(f"[{index}/{len(urls)}] Processing {url}", flush=True)
        try:
            results.append(process_one(str(url)))
        except Exception as exc:
            results.append({
                "request_url": str(url),
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            })

    final = {
        "schema_version": 1,
        "purpose": "public-second-brain-real-source-runtime-test",
        "source_count": len(urls),
        "total_runtime_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "source_count": len(urls),
        "total_runtime_seconds": final["total_runtime_seconds"],
        "statuses": [item.get("status") for item in results],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
