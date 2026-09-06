from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

import httpx

import process_public_social_batch as base

VISION_MODEL = "HuggingFaceTB/SmolVLM-256M-Instruct"
GEMINI_MODEL = os.getenv("SECOND_BRAIN_GEMINI_EVIDENCE_MODEL", "gemini-3.5-flash-lite")

SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {
            "type": "object",
            "properties": {
                "available": {"type": "boolean"},
                "language": {"type": ["string", "null"]},
                "text": {"type": "string"},
                "segments": {"type": "array"},
            },
            "required": ["available", "language", "text", "segments"],
        },
        "visual_evidence": {"type": "array"},
        "mentioned_entities": {"type": "array"},
        "source_claims": {"type": "array"},
        "uncertainties": {"type": "array"},
    },
    "required": ["transcript", "visual_evidence", "mentioned_entities", "source_claims", "uncertainties"],
}

VISUAL_SENSITIVE_TERMS = {
    "trading", "trade", "futures", "stocks", "stock", "options", "forex", "crypto",
    "price action", "technical analysis", "chart", "charts", "candlestick", "nasdaq",
    "s&p", "nq", "e-mini", "emini", "market", "markets", "backtest", "indicator", "indicators",
    "entry", "entries", "stop loss", "take profit", "support", "resistance",
    "order flow", "volume profile", "footprint", "dom", "broker", "platform",
    "dashboard", "spreadsheet", "tutorial", "demo", "walkthrough",
}

YOUTUBE_VISUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "visual_evidence": {"type": "array"},
        "source_claims": {"type": "array"},
        "mentioned_entities": {"type": "array"},
        "uncertainties": {"type": "array"},
    },
    "required": ["visual_evidence", "source_claims", "mentioned_entities", "uncertainties"],
}

YOUTUBE_TECHNICAL_PROMPT = """
This YouTube video is visual-sensitive. Extract BOTH the full faithful transcript and the
decision-relevant visual evidence in one pass.

For trading/finance content, inspect the actual video frames and prioritize:
- chart instrument/symbol and timeframe when visible;
- price levels, overnight highs/lows, support/resistance, entries, stops, targets and annotations;
- indicators, dashboards, statistics, tables and backtest results;
- broker/platform UI and order/execution evidence;
- any visible numbers that materially support or contradict a spoken claim.

Keep transcript wording separate from visual evidence. For visual_evidence, provide timestamps or
time ranges, readable text/numbers, and a literal description. Extract source_claims separately.
Do not infer that a trade was profitable merely because the creator says so. Do not verify against
external data. Record uncertainty instead of guessing. Return only the requested JSON.
""".strip()

YOUTUBE_VISUAL_PROMPT = """
This YouTube video is visual-sensitive. Inspect the actual video frames, not only the audio.

Extract a compact visual timeline of the most decision-relevant evidence. For trading/finance
content, prioritize:
- chart instrument/symbol and timeframe when visible;
- price levels, overnight highs/lows, support/resistance, entries, stops, targets and annotations;
- indicators, dashboards, statistics, tables and backtest results;
- broker/platform UI and order/execution evidence;
- any visible numbers that materially support or contradict a spoken claim.

Each visual_evidence item should include the best timestamp or time range you can identify,
the readable text/numbers, and a literal description of what is visible. Do not infer that a
trade was profitable merely because the creator says so. Do not verify against external data.

Extract source_claims separately when a spoken claim is materially illustrated by a visual.
Mark uncertainty when labels/numbers are unreadable or when the video does not visually prove
the spoken claim. Return only the requested JSON.
""".strip()

PROMPT = """
Extract faithful evidence from this source.
Preserve speech, readable titles/names/text, key visual content, entities actually present,
and claims made by the source. Preserve image order and video timing where possible.
Do not infer the user's preferences, beliefs, intent, goals, or credibility judgments.
Do not add external knowledge. Record uncertainty instead of guessing.
Return only the required JSON.
""".strip()


def canonical(url: str) -> str:
    resolved = base.resolve_url(url)
    parsed = urlparse(resolved)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if "tiktok.com" in host or "instagram.com" in host:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    if "youtube.com" in host:
        vid = dict(parse_qsl(parsed.query)).get("v")
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    return resolved


def youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "youtu.be":
        return parsed.path.strip("/").split("/")[0] or None
    return dict(parse_qsl(parsed.query)).get("v") if "youtube.com" in host else None


def youtube_oembed(url: str) -> dict[str, Any]:
    response = httpx.get(
        "https://www.youtube.com/oembed",
        params={"url": url, "format": "json"},
        timeout=20,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    return {"provider": "youtube-oembed", "title": payload.get("title"), "author": payload.get("author_name")}


def youtube_transcript_api(url: str) -> dict[str, Any]:
    from youtube_transcript_api import YouTubeTranscriptApi

    vid = youtube_id(url)
    if not vid:
        raise RuntimeError("YouTube video ID unavailable")
    rows = list(YouTubeTranscriptApi().fetch(vid, languages=["es", "es-419", "en", "en-US"]))
    segments = []
    for row in rows:
        value = str(getattr(row, "text", "") or "").strip()
        if not value:
            continue
        start = getattr(row, "start", None)
        duration = getattr(row, "duration", None)
        segments.append({
            "start": start,
            "end": start + duration if isinstance(start, (int, float)) and isinstance(duration, (int, float)) else None,
            "text": value,
        })
    return {
        "available": bool(segments),
        "language": None,
        "text": " ".join(item["text"] for item in segments),
        "segments": segments,
        "provenance": "youtube-transcript-api",
    }


@contextmanager
def parth_deadline() -> Any:
    seconds = int(os.getenv("SECOND_BRAIN_PARTH_TIMEOUT_SECONDS", "35"))
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handler(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"parth-dl exceeded {seconds}s route budget")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def parth_download(url: str, root: Path) -> tuple[list[Path], dict[str, Any]]:
    from parth_dl import InstagramDownloader

    root.mkdir(parents=True, exist_ok=True)
    downloader = InstagramDownloader(verbose=False, rate_limit=True, quiet=True, overwrite=True)
    info = downloader.get_info(url)
    result = downloader.download(url, output_path=str(root), output_mode="directory", info=info)

    if result is None:
        paths = sorted(p for p in root.rglob("*") if p.is_file())
    elif isinstance(result, (str, Path)):
        paths = [Path(result)]
    else:
        paths = [Path(p) for p in result]

    files = [p for p in paths if p.is_file() and p.suffix.lower() in base.IMAGE_EXTS | base.VIDEO_EXTS | base.AUDIO_EXTS]
    if not files:
        raise RuntimeError("parth-dl returned no supported media")
    source = {}
    if isinstance(info, dict):
        def info_text(*keys: str) -> str | None:
            for key in keys:
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    rendered = value.strip()
                    if rendered.lower() not in {"detailed", "detail", "unknown", "none"}:
                        return rendered
                if isinstance(value, dict):
                    for nested_key in ("username", "uniqueId", "nickname", "name"):
                        nested = value.get(nested_key)
                        if isinstance(nested, str) and nested.strip():
                            return nested.strip()
            return None

        source = {
            "provider": "parth-dl",
            "title": info_text("title", "description"),
            "author": info_text("username", "uploader", "author", "owner"),
            "post_id": info_text("id", "shortcode"),
            "media_kind": info_text("type"),
        }
    return files, source


class Gemini:
    def __init__(self) -> None:
        from google import genai

        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY missing")
        self.client = genai.Client(api_key=key, http_options={"timeout": 240000})

    def youtube(self, url: str) -> dict[str, Any]:
        return self._request([{"type": "video", "uri": url, "resolution": "medium"}], "youtube_url")

    def youtube_technical(self, url: str) -> dict[str, Any]:
        interaction = self.client.interactions.create(
            model=GEMINI_MODEL,
            input=[
                {"type": "video", "uri": url, "resolution": "medium"},
                {"type": "text", "text": YOUTUBE_TECHNICAL_PROMPT},
            ],
            response_format={"type": "text", "mime_type": "application/json", "schema": SCHEMA},
            generation_config={"thinking_level": "minimal", "max_output_tokens": 32768},
        )
        raw = getattr(interaction, "output_text", None)
        if not raw:
            raise RuntimeError("Gemini returned no technical YouTube evidence")
        evidence = json.loads(raw.strip())
        usage = getattr(interaction, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json")
        return {
            "model": GEMINI_MODEL,
            "mode": "youtube_technical",
            "evidence": evidence,
            "usage": usage,
        }

    def youtube_visual(self, url: str) -> dict[str, Any]:
        interaction = self.client.interactions.create(
            model=GEMINI_MODEL,
            input=[
                {"type": "video", "uri": url, "resolution": "medium"},
                {"type": "text", "text": YOUTUBE_VISUAL_PROMPT},
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": YOUTUBE_VISUAL_SCHEMA,
            },
            generation_config={"thinking_level": "minimal", "max_output_tokens": 16384},
        )
        raw = getattr(interaction, "output_text", None)
        if not raw:
            raise RuntimeError("Gemini returned no focused YouTube visual evidence")
        evidence = json.loads(raw.strip())
        usage = getattr(interaction, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json")
        return {
            "model": GEMINI_MODEL,
            "mode": "youtube_visual_focus",
            "evidence": evidence,
            "usage": usage,
        }

    def media(self, paths: list[Path]) -> dict[str, Any]:
        uploaded = []
        inputs: list[dict[str, Any]] = []
        try:
            for pos, path in enumerate(paths, start=1):
                item = self.client.files.upload(file=str(path))
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    state = getattr(getattr(item, "state", None), "name", None) or str(getattr(item, "state", ""))
                    if state.upper() in {"", "ACTIVE"}:
                        break
                    if state.upper() == "FAILED":
                        raise RuntimeError("Gemini file processing failed")
                    time.sleep(2)
                    item = self.client.files.get(name=item.name)
                uploaded.append(item)
                mime = getattr(item, "mime_type", None) or mimetypes.guess_type(path.name)[0]
                media_type = "image" if (mime or "").startswith("image/") else "video" if (mime or "").startswith("video/") else "audio"
                inputs.append({"type": "text", "text": f"MEDIA_ITEM {pos}/{len(paths)}"})
                media = {"type": media_type, "uri": item.uri, "mime_type": mime}
                if media_type in {"image", "video"}:
                    media["resolution"] = "medium"
                inputs.append(media)
            return self._request(inputs, "uploaded_media")
        finally:
            for item in uploaded:
                try:
                    self.client.files.delete(name=item.name)
                except Exception:
                    pass

    def _request(self, media_inputs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        interaction = self.client.interactions.create(
            model=GEMINI_MODEL,
            input=[*media_inputs, {"type": "text", "text": PROMPT}],
            response_format={"type": "text", "mime_type": "application/json", "schema": SCHEMA},
            generation_config={"thinking_level": "minimal", "max_output_tokens": 32768},
        )
        raw = getattr(interaction, "output_text", None)
        if not raw:
            raise RuntimeError("Gemini returned no evidence")
        evidence = json.loads(raw.strip())
        usage = getattr(interaction, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json")
        return {"model": GEMINI_MODEL, "mode": mode, "evidence": evidence, "usage": usage}


class Vision:
    def __init__(self) -> None:
        import torch
        from PIL import Image
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        self.torch = torch
        self.Image = Image
        self.processor = AutoProcessor.from_pretrained(VISION_MODEL)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            VISION_MODEL,
            torch_dtype=torch.float32,
            _attn_implementation="eager",
        ).to("cpu")
        self.model.eval()

    def describe(self, path: Path) -> str:
        image = self.Image.open(path).convert("RGB")
        image.thumbnail((640, 640), self.Image.Resampling.LANCZOS)
        messages = [{
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": "Describe visible evidence only. Preserve readable titles, names, labels, numbers, objects and actions. Do not infer intent."},
            ],
        }]
        prompt = self.processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = self.processor(text=prompt, images=[image], return_tensors="pt")
        with self.torch.inference_mode():
            generated = self.model.generate(**inputs, do_sample=False, max_new_tokens=96)
        generated = generated[:, inputs["input_ids"].shape[1]:]
        return self.processor.batch_decode(generated, skip_special_tokens=True)[0].strip()


_VISION: Vision | None = None


def vision() -> Vision:
    global _VISION
    if _VISION is None:
        _VISION = Vision()
    return _VISION


def duration(video: Path) -> float:
    completed = base.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        timeout=20,
    )
    try:
        return float(completed.stdout.strip())
    except Exception:
        return 1.0


def frames(video: Path, root: Path) -> list[tuple[float, Path]]:
    root.mkdir(parents=True, exist_ok=True)
    total = duration(video)
    output = []
    for i, frac in enumerate([0.05, 0.25, 0.5, 0.75, 0.95], start=1):
        second = max(0.0, min(total - 0.05, total * frac))
        path = root / f"frame-{i}.jpg"
        completed = base.run(
            ["ffmpeg", "-loglevel", "error", "-ss", f"{second:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(path)],
            timeout=30,
        )
        if completed.returncode == 0 and path.exists():
            output.append((round(second, 3), path))
    return output


def local_evidence(files: list[Path], root: Path) -> dict[str, Any]:
    image_files = [p for p in files if p.suffix.lower() in base.IMAGE_EXTS]
    video_files = [p for p in files if p.suffix.lower() in base.VIDEO_EXTS]
    sensor = vision()
    visual_rows = []
    transcript = {"available": False, "language": None, "text": "", "segments": []}

    for pos, image in enumerate(image_files, start=1):
        visual_rows.append({"position": pos, "ocr_text": base.ocr_image(image), "description": sensor.describe(image)})

    if video_files:
        video = video_files[0]
        analysis = base.analyze_video(video, root / "analysis")
        raw = analysis.get("transcript")
        if isinstance(raw, dict):
            transcript = {
                "available": bool(raw.get("text") or raw.get("segments")),
                "language": raw.get("language"),
                "text": raw.get("text") or "",
                "segments": raw.get("segments") or [],
            }
        elif isinstance(raw, list):
            text_value = " ".join(str(x.get("text") or "") for x in raw if isinstance(x, dict)).strip()
            transcript = {"available": bool(text_value), "language": None, "text": text_value, "segments": raw}

        for second, frame in frames(video, root / "frames"):
            visual_rows.append({"time_seconds": second, "ocr_text": base.ocr_image(frame), "description": sensor.describe(frame)})

    return {
        "transcript": transcript,
        "visual_evidence": visual_rows,
        "mentioned_entities": [],
        "source_claims": [],
        "uncertainties": [],
    }


def has_evidence(evidence: dict[str, Any]) -> bool:
    transcript = evidence.get("transcript")
    return bool(
        (isinstance(transcript, dict) and (transcript.get("available") or transcript.get("text")))
        or evidence.get("visual_evidence")
        or evidence.get("mentioned_entities")
        or evidence.get("source_claims")
    )


def youtube_visual_sensitive(source: dict[str, Any], evidence: dict[str, Any]) -> tuple[bool, list[str]]:
    parts = [
        str(source.get("title") or ""),
        str(source.get("description") or ""),
        str((evidence.get("transcript") or {}).get("text") or "")[:12000],
    ]
    haystack = " ".join(parts).lower()
    matched = sorted(term for term in VISUAL_SENSITIVE_TERMS if term in haystack)
    return bool(matched), matched


def process_youtube(url: str, gemini: Gemini | None) -> dict[str, Any]:
    started = time.monotonic()
    source = {}
    attempts = []
    warnings = []

    for name, fn in (("youtube-oembed", youtube_oembed), ("yt-dlp-metadata", base.ytdlp_metadata)):
        t0 = time.monotonic()
        try:
            source.update(fn(url))
            attempts.append({"method": name, "success": True, "seconds": round(time.monotonic() - t0, 3)})
        except Exception as exc:
            attempts.append({"method": name, "success": False, "seconds": round(time.monotonic() - t0, 3), "error": str(exc)})
            warnings.append(f"{name}: {exc}")

    evidence = {"transcript": {"available": False, "language": None, "text": "", "segments": []}, "visual_evidence": [], "mentioned_entities": [], "source_claims": [], "uncertainties": []}

    t0 = time.monotonic()
    try:
        evidence["transcript"] = youtube_transcript_api(url)
        attempts.append({"method": "youtube-transcript-api", "success": has_evidence(evidence), "seconds": round(time.monotonic() - t0, 3)})
    except Exception as exc:
        attempts.append({"method": "youtube-transcript-api", "success": False, "seconds": round(time.monotonic() - t0, 3), "error": str(exc)})
        warnings.append(f"youtube-transcript-api: {exc}")

    sensors = {}
    pre_visual_sensitive, pre_visual_triggers = youtube_visual_sensitive(source, evidence)
    if gemini:
        t0 = time.monotonic()
        try:
            result = gemini.youtube_technical(url) if pre_visual_sensitive else gemini.youtube(url)
            ge = result["evidence"]
            if has_evidence(evidence):
                for key in ("visual_evidence", "mentioned_entities", "source_claims", "uncertainties"):
                    if ge.get(key):
                        evidence[key] = ge[key]
                if isinstance(ge.get("transcript"), dict) and ge["transcript"].get("available"):
                    evidence["secondary_transcript"] = ge["transcript"]
            else:
                evidence = ge
            sensors["gemini"] = {
                "model": result["model"],
                "mode": result["mode"],
                "usage": result.get("usage"),
            }
            attempts.append({
                "method": "gemini-youtube-technical" if pre_visual_sensitive else "gemini-youtube-url",
                "success": True,
                "seconds": round(time.monotonic() - t0, 3),
            })
        except Exception as exc:
            attempts.append({
                "method": "gemini-youtube-technical" if pre_visual_sensitive else "gemini-youtube-url",
                "success": False,
                "seconds": round(time.monotonic() - t0, 3),
                "error": str(exc),
            })
            warnings.append(f"gemini: {exc}")

    visual_sensitive, visual_triggers = youtube_visual_sensitive(source, evidence)
    if gemini and visual_sensitive and not pre_visual_sensitive and len(evidence.get("visual_evidence") or []) < 3:
        t0 = time.monotonic()
        try:
            focused = gemini.youtube_visual(url)
            focused_evidence = focused["evidence"]
            if focused_evidence.get("visual_evidence"):
                evidence["visual_evidence"] = focused_evidence["visual_evidence"]
            for key in ("source_claims", "mentioned_entities", "uncertainties"):
                if focused_evidence.get(key):
                    existing = list(evidence.get(key) or [])
                    existing.extend(focused_evidence[key])
                    evidence[key] = existing
            sensors["gemini_visual_focus"] = {
                "model": focused["model"],
                "mode": focused["mode"],
                "usage": focused.get("usage"),
            }
            attempts.append({
                "method": "gemini-youtube-visual-focus",
                "success": bool(focused_evidence.get("visual_evidence")),
                "seconds": round(time.monotonic() - t0, 3),
            })
        except Exception as exc:
            attempts.append({
                "method": "gemini-youtube-visual-focus",
                "success": False,
                "seconds": round(time.monotonic() - t0, 3),
                "error": str(exc),
            })
            warnings.append(f"gemini visual focus: {exc}")

    status = "complete" if has_evidence(evidence) else "partial" if source else "failed"
    return {
        "platform": "youtube",
        "status": status,
        "source": source,
        "evidence": evidence,
        "sensors": sensors,
        "visual_sensitive": visual_sensitive,
        "visual_focus_triggers": visual_triggers,
        "acquisition_attempts": attempts,
        "warnings": warnings,
        "runtime_seconds": round(time.monotonic() - started, 3),
    }


def instagram_acquire(url: str, root: Path) -> tuple[list[Path], dict[str, Any], list[dict[str, Any]], list[str]]:
    is_reel = "/reel/" in urlparse(url).path.lower() or "/reels/" in urlparse(url).path.lower()
    routes = ("yt-dlp", "parth-dl", "gallery-dl") if is_reel else ("parth-dl", "yt-dlp", "gallery-dl")
    attempts = []
    warnings = []
    source = {}

    for route in routes:
        t0 = time.monotonic()
        try:
            if route == "yt-dlp":
                files = base.ytdlp_download(url, root / "ytdlp", timeout=30)
            elif route == "parth-dl":
                with parth_deadline():
                    files, source = parth_download(url, root / "parth")
            else:
                files = base.gallery_download(url, root / "gallery", timeout=35)
            attempts.append({"method": route, "success": True, "seconds": round(time.monotonic() - t0, 3)})
            return files, source, attempts, warnings
        except Exception as exc:
            attempts.append({"method": route, "success": False, "seconds": round(time.monotonic() - t0, 3), "error": str(exc)})
            warnings.append(f"{route}: {exc}")
    raise RuntimeError("all Instagram acquisition routes failed")


def process_social(url: str, kind: str, gemini: Gemini | None) -> dict[str, Any]:
    started = time.monotonic()
    attempts = []
    warnings = []
    source = {}

    with tempfile.TemporaryDirectory(prefix="second-brain-public-v2-") as temp:
        root = Path(temp)
        try:
            if kind == "instagram":
                files, source, attempts, warnings = instagram_acquire(url, root)
            else:
                t0 = time.monotonic()
                try:
                    source = base.gallery_probe(url)
                    attempts.append({"method": "gallery-dl-probe", "success": True, "seconds": round(time.monotonic() - t0, 3)})
                except Exception as exc:
                    attempts.append({"method": "gallery-dl-probe", "success": False, "seconds": round(time.monotonic() - t0, 3), "error": str(exc)})
                    warnings.append(f"metadata: {exc}")
                t0 = time.monotonic()
                files = base.gallery_download(url, root / "gallery")
                attempts.append({"method": "gallery-dl-download", "success": True, "seconds": round(time.monotonic() - t0, 3)})
        except Exception as exc:
            return {"platform": kind, "status": "failed", "source": source, "evidence": {}, "sensors": {}, "acquisition_attempts": attempts, "warnings": [*warnings, str(exc)], "runtime_seconds": round(time.monotonic() - started, 3)}

        evidence = None
        sensors = {}

        if gemini:
            t0 = time.monotonic()
            try:
                result = gemini.media(files)
                evidence = result["evidence"]
                sensors["gemini"] = {"model": result["model"], "mode": result["mode"], "usage": result.get("usage")}
                attempts.append({"method": "gemini-uploaded-media", "success": True, "seconds": round(time.monotonic() - t0, 3)})
            except Exception as exc:
                attempts.append({"method": "gemini-uploaded-media", "success": False, "seconds": round(time.monotonic() - t0, 3), "error": str(exc)})
                warnings.append(f"gemini: {exc}")

        if evidence is None or not has_evidence(evidence):
            t0 = time.monotonic()
            try:
                local = local_evidence(files, root)
                if evidence is None or has_evidence(local):
                    evidence = local
                sensors["open_source"] = {"vision": VISION_MODEL, "ocr": "tesseract", "speech": "mcp-video-analyzer"}
                attempts.append({"method": "local-ocr-smolvlm-whisper", "success": has_evidence(local), "seconds": round(time.monotonic() - t0, 3)})
            except Exception as exc:
                attempts.append({"method": "local-ocr-smolvlm-whisper", "success": False, "seconds": round(time.monotonic() - t0, 3), "error": str(exc)})
                warnings.append(f"local sensors: {exc}")

        evidence = evidence or {"transcript": {"available": False, "language": None, "text": "", "segments": []}, "visual_evidence": [], "mentioned_entities": [], "source_claims": [], "uncertainties": []}
        assets = [{"position": i, "media_type": "image" if p.suffix.lower() in base.IMAGE_EXTS else "video" if p.suffix.lower() in base.VIDEO_EXTS else "audio", "size_bytes": p.stat().st_size} for i, p in enumerate(files, start=1)]
        return {"platform": kind, "status": "complete" if has_evidence(evidence) else "partial", "source": source, "assets": {"items": assets, "raw_media_persisted": False}, "evidence": evidence, "sensors": sensors, "acquisition_attempts": attempts, "warnings": warnings, "runtime_seconds": round(time.monotonic() - started, 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    urls = payload["urls"]

    gemini = None
    gemini_error = None
    if os.getenv("GEMINI_API_KEY"):
        try:
            gemini = Gemini()
        except Exception as exc:
            gemini_error = str(exc)

    started = time.monotonic()
    cache = {}
    results = []

    for index, original in enumerate(urls, start=1):
        url = canonical(str(original))
        print(f"[{index}/{len(urls)}] {url}", flush=True)
        if url in cache:
            results.append({"request_url": str(original), "canonical_url": url, "status": "duplicate", "runtime_seconds": 0.0})
            continue

        kind = base.platform(url)
        result = process_youtube(url, gemini) if kind == "youtube" else process_social(url, kind, gemini)
        result["request_url"] = str(original)
        result["canonical_url"] = url
        cache[url] = result
        results.append(result)

    final = {
        "schema_version": 2,
        "purpose": "full-public-second-brain-six-source-test",
        "gemini_secret_configured": bool(os.getenv("GEMINI_API_KEY")),
        "gemini_sensor_initialized": gemini is not None,
        "gemini_initialization_error": gemini_error,
        "input_count": len(urls),
        "unique_source_count": len(cache),
        "total_runtime_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"gemini_secret_configured": final["gemini_secret_configured"], "unique_source_count": final["unique_source_count"], "total_runtime_seconds": final["total_runtime_seconds"], "statuses": [r.get("status") for r in results]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
