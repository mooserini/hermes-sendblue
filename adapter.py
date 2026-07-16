"""
Sendblue platform adapter for Hermes Agent.

Install this directory as ~/.hermes/plugins/sendblue and Hermes can
receive and send iMessage/SMS/RCS through Sendblue from the gateway loop.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
)

# strip_markdown was consolidated into gateway.platforms.helpers when the
# per-adapter copies in sms.py/bluebubbles.py/feishu.py were de-duplicated.
# Import it when present (current cores) but fall back to a local copy on
# pre-consolidation cores, so a missing symbol can never disable the whole
# plugin at import time (cf. the cache_media_bytes regression). The fallback
# mirrors core's regexes exactly: code content is kept (only fences removed)
# and markdown links collapse to their anchor text.
try:
    from gateway.platforms.helpers import strip_markdown
except Exception:  # pragma: no cover - exercised only on very old cores
    import re as _re

    _MD_FALLBACK_PATTERNS = [
        (_re.compile(r"\*\*(.+?)\*\*", _re.DOTALL), r"\1"),                       # bold
        (_re.compile(r"\*(.+?)\*", _re.DOTALL), r"\1"),                           # italic *
        (_re.compile(r"\b__(?![\s_])(.+?)(?<![\s_])__\b", _re.DOTALL), r"\1"),    # bold _
        (_re.compile(r"\b_(?![\s_])(.+?)(?<![\s_])_\b", _re.DOTALL), r"\1"),      # italic _
        (_re.compile(r"```[a-zA-Z0-9_+-]*\n?"), ""),                              # code fences (keep body)
        (_re.compile(r"`(.+?)`"), r"\1"),                                         # inline code
        (_re.compile(r"^#{1,6}\s+", _re.MULTILINE), ""),                          # ATX headings
        (_re.compile(r"\[([^\]]+)\]\([^\)]+\)"), r"\1"),                          # links -> anchor text
        (_re.compile(r"\n{3,}"), "\n\n"),                                         # collapse blank runs
    ]

    def strip_markdown(text: str) -> str:
        text = str(text or "")
        for pattern, repl in _MD_FALLBACK_PATTERNS:
            text = pattern.sub(repl, text)
        return text.strip()

logger = logging.getLogger("gateway.platforms.sendblue")

DEFAULT_API_BASE = "https://api.sendblue.com"
DEFAULT_USER_AGENT = "sendblue-hermes/1.0 (+https://github.com/njerschow/hermes-sendblue)"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_LOOKBACK_SECONDS = 60
DEFAULT_LIMIT = 100
MAX_SENDBLUE_BODY_CHARS = 18_000
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
MIN_WEBHOOK_SECRET_CHARS = 16
SENDBLUE_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # Sendblue documented per-file limit
SENDBLUE_MEDIA_DOWNLOAD_TIMEOUT = 30.0  # seconds; matches base.py URL-cache helpers
SENDBLUE_MAX_INBOUND_MEDIA_BYTES = 25 * 1024 * 1024  # bound each inbound media download

# Companion cadence: multi-bubble text replies with typing + thought pauses.
DEFAULT_CHUNKED_REPLIES = True
DEFAULT_CHUNK_MAX = 4
DEFAULT_CHUNK_DELAY_MIN_MS = 1100
DEFAULT_CHUNK_DELAY_MAX_MS = 2800
DEFAULT_CHUNK_TYPING = True
# Don't bother multi-bubbling tiny one-liners (still allow explicit \n\n splits).
CHUNK_MIN_TEXT_CHARS = 72
CHUNK_MIN_BEAT_CHARS = 10

# Sendblue inbound messages carry no media-type metadata (only a bare media_url),
# so we download the bytes and let _classify_and_cache() classify them, then map the
# resulting kind to a Hermes MessageType. All audio -> VOICE (not AUDIO) so iMessage
# voice notes are auto-transcribed; the tradeoff is that a plain audio clip is also
# sent to STT, but Sendblue exposes no "is voice note" flag and silently dropping
# real voice notes is the worse failure. This mirrors the BlueBubbles adapter.
_KIND_TO_MESSAGE_TYPE = {
    "image": MessageType.PHOTO,
    "video": MessageType.VIDEO,
    "audio": MessageType.VOICE,
    "document": MessageType.DOCUMENT,
}
_KIND_TO_PLACEHOLDER = {
    "image": "(image)",
    "video": "(video)",
    "audio": "(voice message)",
    "document": "(attachment)",
}

# MIME / extension → kind classification for inbound media. The unified
# cache_media_bytes() dispatcher only landed in core after May 2026 (and even on
# current main its sole caller is telegram.py), so we classify here and route to
# the long-stable per-kind cache_*_from_bytes helpers — the same ones every
# bundled adapter (BlueBubbles, Photon, …) uses. Works on old and new cores.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tiff"}
_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".wav", ".caf", ".aac", ".opus", ".flac", ".amr"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".3gp"}
_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".jpg", "image/heif": ".jpg",
    "image/tiff": ".jpg", "image/bmp": ".bmp",
}
_AUDIO_EXT_BY_MIME = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".m4a", "audio/ogg": ".ogg", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/x-caf": ".caf", "audio/amr": ".amr",
}
_VIDEO_EXT_BY_MIME = {
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "video/webm": ".webm", "video/3gpp": ".3gp",
}


def _classify_and_cache(data: bytes, filename: str, mime: str) -> Optional[tuple]:
    """Cache inbound media bytes locally; return (path, media_type, kind) or None.

    Classifies by MIME prefix then file extension and routes to the per-kind
    cache_*_from_bytes helpers. Returns None when the bytes can't be classified
    (the caller then falls back to the ``[Media: <url>]`` marker). ``kind`` is one
    of "image" / "audio" / "video" / "document".

    Apple iMessage voice notes arrive from Sendblue as CAF. Hermes STT does not
    accept ``.caf``, so inbound CAF is best-effort transcoded to M4A before
    caching whenever ffmpeg is available.
    """
    mime = (mime or "").lower()
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if mime.startswith("image/") or ext in _IMAGE_EXTS:
            chosen = ext if ext in _IMAGE_EXTS else _IMAGE_EXT_BY_MIME.get(mime, ".jpg")
            try:
                return cache_image_from_bytes(data, chosen), mime or "image/jpeg", "image"
            except ValueError:
                return None  # claimed to be an image but the bytes aren't one
        if mime.startswith("audio/") or ext in _AUDIO_EXTS:
            is_caf = ext == ".caf" or "caf" in mime
            if is_caf:
                converted = _transcode_caf_bytes_to_m4a(data)
                if converted is not None:
                    data, chosen = converted
                    mime = "audio/mp4"
                    logger.info(
                        "Transcoded inbound CAF voice note to M4A for STT (%d bytes)",
                        len(data),
                    )
                else:
                    chosen = ".caf"
                    logger.warning(
                        "Inbound CAF could not be transcoded; STT may reject .caf"
                    )
            else:
                chosen = ext if ext in _AUDIO_EXTS else _AUDIO_EXT_BY_MIME.get(mime, ".ogg")
            return cache_audio_from_bytes(data, chosen), mime or "audio/mpeg", "audio"
        if mime.startswith("video/") or ext in _VIDEO_EXTS:
            chosen = ext if ext in _VIDEO_EXTS else _VIDEO_EXT_BY_MIME.get(mime, ".mp4")
            return cache_video_from_bytes(data, chosen), mime or "video/mp4", "video"
        if ext:
            # A named file of some other type → treat as a document.
            return cache_document_from_bytes(data, filename), mime or "application/octet-stream", "document"
        # No MIME and no extension: last-resort image sniff (validates magic bytes).
        try:
            return cache_image_from_bytes(data, ".jpg"), "image/jpeg", "image"
        except ValueError:
            return None
    except Exception:
        return None


def _is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return ""


def _normalize_phone(value: str) -> str:
    """Normalize common phone input to E.164-ish form without guessing country."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        return "+" + "".join(ch for ch in raw[1:] if ch.isdigit())
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return f"+{digits}" if digits else raw


def _redacted_phone(value: str) -> str:
    normalized = _normalize_phone(value)
    if len(normalized) <= 5:
        return "unknown"
    return f"...{normalized[-4:]}"


def _is_valid_webhook_secret(value: str) -> bool:
    secret = str(value or "").strip()
    return bool(secret) and secret.lower() != "change-me" and len(secret) >= MIN_WEBHOOK_SECRET_CHARS


def _split_message(text: str, limit: int = MAX_SENDBLUE_BODY_CHARS) -> List[str]:
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < limit // 3:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 3:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 3:
            split_at = limit

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return [chunk for chunk in chunks if chunk]


def _split_sentences(text: str) -> List[str]:
    """Split prose into sentence-ish beats, keeping terminal punctuation."""
    text = str(text or "").strip()
    if not text:
        return []
    # Prefer hard sentence ends; also treat em/en dash pauses as soft boundaries
    # when the model writes run-on companion prose.
    parts = re.split(r"(?<=[.!?…])\s+|(?<=[.!?…])(?=[\"'”’])\s*", text)
    sentences = [p.strip() for p in parts if p and p.strip()]
    if len(sentences) <= 1:
        return [text]

    # Fold tiny fragments into the previous beat (e.g. "lol." "ok.")
    merged: List[str] = []
    for s in sentences:
        if merged and len(s) < CHUNK_MIN_BEAT_CHARS:
            merged[-1] = f"{merged[-1]} {s}".strip()
        else:
            merged.append(s)
    return merged or [text]


def _split_chat_beats(text: str, *, max_beats: int = DEFAULT_CHUNK_MAX) -> List[str]:
    """Post-split a full reply into short iMessage-style texting beats.

    Prefer explicit paragraph breaks (``\\n\\n``). Otherwise sentence-split.
    Cap at ``max_beats`` so a long essay doesn't become a firehose.
    """
    text = str(text or "").strip()
    if not text:
        return []

    max_beats = max(1, min(int(max_beats or DEFAULT_CHUNK_MAX), 8))

    # Explicit multi-paragraph authoring always wins.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]

    if len(paragraphs) <= 1:
        single = paragraphs[0] if paragraphs else text
        # Short one-liners stay one bubble — no fake multi-send for "ok cool".
        if len(single) < CHUNK_MIN_TEXT_CHARS and "\n" not in single:
            return _split_message(single)
        # Single-newline soft breaks: treat as weak paragraph hints.
        if "\n" in single and "\n\n" not in text:
            line_parts = [ln.strip() for ln in single.split("\n") if ln.strip()]
            if 2 <= len(line_parts) <= max_beats:
                paragraphs = line_parts
            else:
                paragraphs = _split_sentences(single)
        else:
            paragraphs = _split_sentences(single)

    if len(paragraphs) == 1:
        return _split_message(paragraphs[0])

    # Cap: keep the first (max_beats - 1) beats, fold remainder into the last.
    if len(paragraphs) > max_beats:
        head = paragraphs[: max_beats - 1]
        tail = " ".join(paragraphs[max_beats - 1 :]).strip()
        paragraphs = head + ([tail] if tail else [])

    # Absorb near-empty orphans into the previous bubble.
    beats: List[str] = []
    for part in paragraphs:
        if beats and len(part) < CHUNK_MIN_BEAT_CHARS:
            beats[-1] = f"{beats[-1]} {part}".strip()
        else:
            beats.append(part)

    # Hard length safety per bubble (Sendblue body limit).
    out: List[str] = []
    for beat in beats:
        out.extend(_split_message(beat))

    if len(out) > max_beats:
        head = out[: max_beats - 1]
        tail = "\n\n".join(out[max_beats - 1 :]).strip()
        out = head + ([tail] if tail else [])

    return out or _split_message(text)


def _chunk_pause_seconds(min_ms: int, max_ms: int) -> float:
    """Random thought-pause between bubbles (seconds)."""
    lo = max(0, int(min_ms))
    hi = max(lo, int(max_ms))
    if hi <= 0:
        return 0.0
    return random.uniform(lo / 1000.0, hi / 1000.0)


def _parse_datetime(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_local_media(value: Any) -> bool:
    text = str(value or "")
    return bool(text) and not text.startswith(("http://", "https://"))


def _normalize_upload_filename(filename: str) -> str:
    """Return a multipart-safe upload name with a lowercased extension.

    Hermes historically matched ``MEDIA:`` tags with a case-sensitive extension
    regex (fixed on newer cores, still worth defending). Sendblue also uses the
    upload filename as a type hint. Keep the stem intact — including spaces —
    but force the extension to lowercase so ``Photo.JPG`` / ``Audio Message.CAF``
    upload as deliverable names.
    """
    raw = str(filename or "").strip() or "file"
    name = Path(raw).name.replace("\r", "").replace("\n", "").replace('"', "")
    if not name or name in {".", ".."}:
        name = "file"
    stem, ext = os.path.splitext(name)
    if not stem:
        stem = "file"
    return f"{stem}{ext.lower()}"


def _run_ffmpeg(
    source: Path,
    dest: Path,
    *,
    audio_args: List[str],
) -> bool:
    """Run ffmpeg with the given audio codec args. Returns True on success."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                *audio_args,
                str(dest),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        return result.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _run_ffmpeg_to_m4a(source: Path, dest: Path) -> bool:
    """Transcode *source* audio to AAC/M4A at *dest*. Returns True on success."""
    return _run_ffmpeg(
        source,
        dest,
        audio_args=["-c:a", "aac", "-b:a", "128k"],
    )


def _run_ffmpeg_to_caf(source: Path, dest: Path) -> bool:
    """Transcode *source* audio to Apple CAF (PCM) at *dest*.

    Sendblue renders ``.caf`` ``media_url`` values as native iMessage voice
    notes (inline bubble). Linear PCM in a CAF container is widely accepted.
    """
    return _run_ffmpeg(
        source,
        dest,
        # Mono 24 kHz PCM keeps voice notes compact while remaining iMessage-friendly.
        audio_args=["-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1"],
    )


def _transcode_to_caf(source: Path) -> Optional[Path]:
    """Best-effort convert audio to Apple CAF for native iMessage voice notes.

    Returns a path inside a fresh temp directory on success, or ``None``.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="sendblue-voice-"))
    # Sendblue's docs use this style of object name for voice notes.
    dest = tmp_dir / "Audio Message.caf"
    if not _run_ffmpeg_to_caf(source, dest):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    return dest


def _transcode_caf_bytes_to_m4a(data: bytes) -> Optional[Tuple[bytes, str]]:
    """Best-effort convert inbound CAF bytes to M4A for Hermes STT.

    Hermes transcription accepts ``.m4a`` but not ``.caf``. Returns
    ``(m4a_bytes, ".m4a")`` on success, or ``None`` to keep the original CAF.
    """
    if not data:
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="sendblue-caf-in-"))
    try:
        source = tmp_dir / "input.caf"
        dest = tmp_dir / "output.m4a"
        source.write_bytes(data)
        if not _run_ffmpeg_to_m4a(source, dest):
            return None
        return dest.read_bytes(), ".m4a"
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _prepare_local_media_for_upload(
    file_path: str | Path,
    *,
    as_voice_note: bool = False,
) -> Tuple[Path, str, Optional[Path]]:
    """Normalize a local media path before Sendblue upload.

    Returns ``(path_to_read, upload_filename, cleanup_path)``.
    ``cleanup_path`` is a temp directory the caller must remove after upload.

    When ``as_voice_note`` is True (Hermes ``send_voice`` / auto-TTS path),
    non-CAF audio is best-effort transcoded to Apple CAF so Sendblue delivers
    a native iMessage voice bubble rather than a generic file attachment.
    """
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Local media path not found: {file_path}")

    read_path = path
    upload_name = _normalize_upload_filename(path.name)
    cleanup: Optional[Path] = None

    if as_voice_note:
        if path.suffix.lower() == ".caf":
            # Already CAF — keep bytes, use a voice-note-friendly upload name.
            upload_name = "Audio Message.caf"
        else:
            converted = _transcode_to_caf(path)
            if converted is not None:
                read_path = converted
                upload_name = "Audio Message.caf"
                cleanup = converted.parent
                logger.info(
                    "Transcoded %s -> Audio Message.caf for native iMessage voice note",
                    path.name,
                )
            else:
                logger.warning(
                    "Voice-note CAF encode failed for %s; uploading as attachment (%s)",
                    path.name,
                    upload_name,
                )

    return read_path, upload_name, cleanup


def _cleanup_prepared_media(cleanup: Optional[Path]) -> None:
    if cleanup is None:
        return
    try:
        if cleanup.is_dir():
            shutil.rmtree(cleanup, ignore_errors=True)
        elif cleanup.is_file():
            cleanup.unlink(missing_ok=True)
    except Exception:
        logger.debug("Failed to clean temp media %s", cleanup, exc_info=True)


def _build_multipart(filename: str, data: bytes, boundary: str) -> bytes:
    safe_name = _normalize_upload_filename(filename).replace('"', "").replace("\r", "").replace("\n", "")
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return header + data + footer


def _hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()


@dataclass
class SendblueSettings:
    api_key: str
    api_secret: str
    phone_number: str
    api_base: str = DEFAULT_API_BASE
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    startup_lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS
    message_limit: int = DEFAULT_LIMIT
    mark_read: bool = False
    webhook_enabled: bool = False
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 3141
    webhook_path: str = "/webhook/sendblue"
    webhook_secret: str = ""
    # Companion cadence — multi-bubble post-split with typing + thought pauses
    chunked_replies: bool = DEFAULT_CHUNKED_REPLIES
    chunk_max: int = DEFAULT_CHUNK_MAX
    chunk_delay_min_ms: int = DEFAULT_CHUNK_DELAY_MIN_MS
    chunk_delay_max_ms: int = DEFAULT_CHUNK_DELAY_MAX_MS
    chunk_typing: bool = DEFAULT_CHUNK_TYPING


def _settings_from_config(config: Any) -> SendblueSettings:
    extra = getattr(config, "extra", {}) or {}

    api_key = (
        _env_first("SENDBLUE_API_KEY", "SENDBLUE_API_API_KEY")
        or extra.get("api_key")
        or extra.get("apiKey")
        or ""
    )
    api_secret = (
        _env_first("SENDBLUE_API_SECRET", "SENDBLUE_API_API_SECRET")
        or extra.get("api_secret")
        or extra.get("apiSecret")
        or ""
    )
    phone_number = (
        _env_first("SENDBLUE_PHONE_NUMBER", "SENDBLUE_NUMBER")
        or extra.get("phone_number")
        or extra.get("phoneNumber")
        or extra.get("sendblue_number")
        or ""
    )

    api_base = os.getenv("SENDBLUE_API_BASE") or extra.get("api_base") or DEFAULT_API_BASE
    poll_interval = os.getenv("SENDBLUE_POLL_INTERVAL_SECONDS") or extra.get("poll_interval_seconds")
    lookback = os.getenv("SENDBLUE_STARTUP_LOOKBACK_SECONDS") or extra.get("startup_lookback_seconds")
    limit = os.getenv("SENDBLUE_MESSAGE_LIMIT") or extra.get("message_limit")
    webhook = extra.get("webhook", {}) if isinstance(extra.get("webhook"), dict) else {}

    # Chunked replies: default ON for companion cadence. Explicit false disables.
    chunked_raw = os.getenv("SENDBLUE_CHUNKED_REPLIES")
    if chunked_raw is None and "chunked_replies" not in extra:
        chunked_replies = DEFAULT_CHUNKED_REPLIES
    else:
        chunked_replies = _is_truthy(
            chunked_raw if chunked_raw is not None else extra.get("chunked_replies")
        )

    chunk_max = int(
        os.getenv("SENDBLUE_CHUNK_MAX")
        or extra.get("chunk_max")
        or DEFAULT_CHUNK_MAX
    )
    chunk_delay_min_ms = int(
        os.getenv("SENDBLUE_CHUNK_DELAY_MIN_MS")
        or extra.get("chunk_delay_min_ms")
        or DEFAULT_CHUNK_DELAY_MIN_MS
    )
    chunk_delay_max_ms = int(
        os.getenv("SENDBLUE_CHUNK_DELAY_MAX_MS")
        or extra.get("chunk_delay_max_ms")
        or DEFAULT_CHUNK_DELAY_MAX_MS
    )
    chunk_typing_raw = os.getenv("SENDBLUE_CHUNK_TYPING")
    if chunk_typing_raw is None and "chunk_typing" not in extra:
        chunk_typing = DEFAULT_CHUNK_TYPING
    else:
        chunk_typing = _is_truthy(
            chunk_typing_raw if chunk_typing_raw is not None else extra.get("chunk_typing")
        )

    return SendblueSettings(
        api_key=str(api_key).strip(),
        api_secret=str(api_secret).strip(),
        phone_number=_normalize_phone(str(phone_number)),
        api_base=str(api_base).rstrip("/"),
        poll_interval_seconds=float(poll_interval or DEFAULT_POLL_INTERVAL_SECONDS),
        startup_lookback_seconds=int(lookback or DEFAULT_LOOKBACK_SECONDS),
        message_limit=int(limit or DEFAULT_LIMIT),
        mark_read=_is_truthy(os.getenv("SENDBLUE_MARK_READ") or extra.get("mark_read")),
        webhook_enabled=_is_truthy(
            os.getenv("SENDBLUE_WEBHOOK_ENABLED")
            or webhook.get("enabled")
            or extra.get("webhook_enabled")
        ),
        webhook_host=str(
            os.getenv("SENDBLUE_WEBHOOK_HOST")
            or webhook.get("host")
            or extra.get("webhook_host")
            or "127.0.0.1"
        ),
        webhook_port=int(
            os.getenv("SENDBLUE_WEBHOOK_PORT")
            or webhook.get("port")
            or extra.get("webhook_port")
            or 3141
        ),
        webhook_path=str(
            os.getenv("SENDBLUE_WEBHOOK_PATH")
            or webhook.get("path")
            or extra.get("webhook_path")
            or "/webhook/sendblue"
        ),
        webhook_secret=str(
            os.getenv("SENDBLUE_WEBHOOK_SECRET")
            or webhook.get("secret")
            or extra.get("webhook_secret")
            or ""
        ),
        chunked_replies=chunked_replies,
        chunk_max=max(1, min(chunk_max, 8)),
        chunk_delay_min_ms=max(0, chunk_delay_min_ms),
        chunk_delay_max_ms=max(0, chunk_delay_max_ms),
        chunk_typing=chunk_typing,
    )


class SendblueClient:
    def __init__(self, settings: SendblueSettings):
        self.settings = settings

    def _headers(self, *, content_type: Optional[str] = "application/json") -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            "sb-api-key-id": self.settings.api_key,
            "sb-api-secret-key": self.settings.api_secret,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def _json_request_sync(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.settings.api_base}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"

        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=payload, headers=self._headers(), method=method)
        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Sendblue API error {exc.code}: {raw}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Sendblue API connection error: {exc}") from exc

    async def list_inbound_messages(self, since: datetime) -> List[Dict[str, Any]]:
        cutoff = since - timedelta(seconds=2)
        limit = max(1, min(self.settings.message_limit, 100))
        query: Dict[str, str] = {
            "limit": str(limit),
            "offset": "0",
            "is_outbound": "false",
            "created_at_gte": _iso_utc(cutoff),
            "order_by": "createdAt",
            "order_direction": "asc",
        }
        if self.settings.phone_number:
            query["sendblue_number"] = self.settings.phone_number

        messages: List[Dict[str, Any]] = []
        offset = 0
        while True:
            query["offset"] = str(offset)
            payload = await asyncio.to_thread(
                self._json_request_sync,
                "GET",
                "/api/v2/messages",
                query=query,
            )
            data = payload.get("data") or []
            messages.extend(
                msg
                for msg in data
                if not msg.get("is_outbound")
                and _parse_datetime(msg.get("date_sent") or msg.get("date_created")) >= cutoff
            )

            pagination = payload.get("pagination") or {}
            total = int(pagination.get("total") or 0)
            if len(data) < limit or (total and offset + len(data) >= total):
                break
            offset += len(data)

        messages.sort(key=lambda msg: _parse_datetime(msg.get("date_sent") or msg.get("date_created")))
        return messages

    async def send_message(
        self,
        to_number: str,
        content: str = "",
        *,
        media_url: str = "",
    ) -> str:
        body: Dict[str, Any] = {
            "number": _normalize_phone(to_number),
            "from_number": self.settings.phone_number,
        }
        if content:
            body["content"] = content
        if media_url:
            body["media_url"] = media_url
        if not body.get("content") and not body.get("media_url"):
            raise ValueError("Sendblue message requires content or media_url")

        payload = await asyncio.to_thread(
            self._json_request_sync,
            "POST",
            "/api/send-message",
            body=body,
        )
        return str(payload.get("message_handle") or payload.get("id") or int(time.time() * 1000))

    async def send_typing_indicator(
        self,
        to_number: str,
        *,
        state: str = "start",
        max_duration_ms: Optional[int] = None,
    ) -> None:
        """Show (or stop) the iMessage ”…” bubble for *to_number*.

        Sendblue typing-v2 accepts ``state`` (``start``/``stop``) and
        ``max_duration_ms`` so the indicator can cover an entire thought-pause
        between multi-bubble replies. Older worker firmware may reject the
        extended fields — we fall back to a bare start request in that case.
        """
        number = _normalize_phone(to_number)
        body: Dict[str, Any] = {
            "number": number,
            "from_number": self.settings.phone_number,
            "state": state if state in {"start", "stop"} else "start",
        }
        if max_duration_ms is not None and body["state"] == "start":
            # API range: 1 .. 300000 ms
            body["max_duration_ms"] = max(1, min(int(max_duration_ms), 300_000))

        try:
            await asyncio.to_thread(
                self._json_request_sync,
                "POST",
                "/api/send-typing-indicator",
                body=body,
            )
        except Exception as first_exc:
            # Legacy workers: strip typing-v2 fields and retry once.
            if "max_duration_ms" in body or body.get("state") != "start":
                legacy = {
                    "number": number,
                    "from_number": self.settings.phone_number,
                }
                try:
                    await asyncio.to_thread(
                        self._json_request_sync,
                        "POST",
                        "/api/send-typing-indicator",
                        body=legacy,
                    )
                    return
                except Exception:
                    pass
            raise first_exc

    async def mark_read(self, to_number: str) -> None:
        body = {
            "number": _normalize_phone(to_number),
            "from_number": self.settings.phone_number,
        }
        await asyncio.to_thread(
            self._json_request_sync,
            "POST",
            "/api/mark-read",
            body=body,
        )

    def _raw_post_sync(
        self,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        *,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        url = f"{self.settings.api_base}{path}"
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Sendblue API error {exc.code}: {raw}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Sendblue API connection error: {exc}") from exc

    async def upload_file_from_bytes(self, filename: str, data: bytes) -> str:
        filename = _normalize_upload_filename(filename)
        if not data:
            raise ValueError(f"File '{filename}' is empty")
        if len(data) > SENDBLUE_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"File '{filename}' is {len(data)} bytes, exceeds Sendblue limit "
                f"of {SENDBLUE_MAX_UPLOAD_BYTES} bytes"
            )

        boundary = f"----SendblueUpload{int(time.time() * 1000)}{os.urandom(8).hex()}"
        body = _build_multipart(filename, data, boundary)
        headers = self._headers(content_type=None)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        payload = await asyncio.to_thread(
            self._raw_post_sync, "/api/upload-file", headers, body
        )
        media_url = payload.get("media_url")
        if not media_url:
            raise RuntimeError(f"Sendblue upload returned no media_url: {payload}")
        return str(media_url)

    async def upload_file(self, file_path: str, *, as_voice_note: bool = False) -> str:
        read_path, upload_name, cleanup = _prepare_local_media_for_upload(
            file_path, as_voice_note=as_voice_note
        )
        try:
            data = await asyncio.to_thread(read_path.read_bytes)
            return await self.upload_file_from_bytes(upload_name, data)
        finally:
            _cleanup_prepared_media(cleanup)

    def _download_media_sync(
        self, media_url: str, *, authenticated: bool = False
    ) -> tuple[bytes, str]:
        # Sendblue CDN media URLs are always HTTPS. Requiring https blocks
        # file://, ftp://, etc. We deliberately skip a full SSRF/DNS check
        # (is_safe_url lives in tools.url_safety, outside gateway.platforms.base):
        # the URL comes from Sendblue's own API response, not user free-text, and a
        # per-poll DNS resolution is avoidable overhead. https-only is the pragmatic
        # guard for a platform plugin.
        parsed = parse.urlparse(media_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError(f"Refusing non-HTTPS media URL: {media_url}")
        # Sendblue may return an object name containing spaces (for example,
        # "Audio Message.caf"). urllib rejects raw control/space characters,
        # so preserve an already-escaped path while encoding the unsafe pieces.
        media_url = parse.urlunparse(
            parsed._replace(path=parse.quote(parsed.path, safe="/%"))
        )

        # The download targets the CDN host, not api.sendblue.com, so by default we
        # do NOT send sb-api-key credentials (avoid leaking them to the CDN). Flip
        # `authenticated` if Sendblue's CDN is ever found to require auth.
        if authenticated:
            headers = self._headers(content_type=None)
        else:
            headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}

        req = request.Request(media_url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=SENDBLUE_MEDIA_DOWNLOAD_TIMEOUT) as resp:
                # Read one byte past the cap so an oversized file is detectable.
                data = resp.read(SENDBLUE_MAX_INBOUND_MEDIA_BYTES + 1)
                if len(data) > SENDBLUE_MAX_INBOUND_MEDIA_BYTES:
                    raise RuntimeError(
                        f"Inbound media exceeds {SENDBLUE_MAX_INBOUND_MEDIA_BYTES} bytes"
                    )
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
                return data, content_type.strip().lower()
        except error.HTTPError as exc:
            raise RuntimeError(f"Sendblue media download error {exc.code}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Sendblue media download connection error: {exc}") from exc

    async def download_media(
        self, media_url: str, *, authenticated: bool = False
    ) -> tuple[bytes, str]:
        """Download inbound media bytes; return (data, lowercased content-type)."""
        return await asyncio.to_thread(
            self._download_media_sync, media_url, authenticated=authenticated
        )


class ProcessedMessageStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
              message_id TEXT PRIMARY KEY,
              processed_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def try_mark(self, message_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
                (message_id, int(time.time())),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def cleanup(self, older_than_seconds: int = 7 * 24 * 60 * 60) -> None:
        cutoff = int(time.time()) - older_than_seconds
        with self._lock:
            self._conn.execute("DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SendblueWebhookServer:
    def __init__(self, adapter: "SendblueAdapter", settings: SendblueSettings):
        self.adapter = adapter
        self.settings = settings
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        adapter = self.adapter
        settings = self.settings
        loop = asyncio.get_running_loop()

        class Handler(BaseHTTPRequestHandler):
            server_version = "HermesSendblueWebhook/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("Sendblue webhook: " + fmt, *args)

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_json(200, {"status": "ok"})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                if path != settings.webhook_path:
                    self._send_json(404, {"error": "not found"})
                    return

                if settings.webhook_secret:
                    provided = (
                        self.headers.get("sb-signing-secret")
                        or self.headers.get("x-sendblue-secret")
                        or self.headers.get("x-webhook-secret")
                        or self.headers.get("authorization")
                        or ""
                    )
                    if provided.startswith("Bearer "):
                        provided = provided[7:]
                    if not hmac.compare_digest(provided, settings.webhook_secret):
                        self._send_json(401, {"error": "unauthorized"})
                        return

                try:
                    length = int(self.headers.get("content-length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "bad content-length"})
                    return

                if length < 1 or length > MAX_WEBHOOK_BODY_BYTES:
                    self._send_json(413, {"error": "payload too large"})
                    return

                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    self._send_json(400, {"error": "invalid json"})
                    return

                if not isinstance(payload, dict):
                    self._send_json(400, {"error": "invalid payload"})
                    return

                self._send_json(200, {"received": True})
                asyncio.run_coroutine_threadsafe(adapter._handle_sendblue_message(payload), loop)

        self.httpd = ThreadingHTTPServer((settings.webhook_host, settings.webhook_port), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="sendblue-webhook",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            self.thread = None


class SendblueAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, **_: Any):
        super().__init__(config=config, platform=Platform("sendblue"))
        self.settings = _settings_from_config(config)
        self.client = SendblueClient(self.settings)
        self._poll_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._webhook_server: Optional[SendblueWebhookServer] = None
        self._last_poll_time = datetime.now(timezone.utc) - timedelta(
            seconds=max(0, self.settings.startup_lookback_seconds)
        )
        state_path = _hermes_home() / "platforms" / "sendblue" / "processed.sqlite3"
        self._store = ProcessedMessageStore(state_path)

    @property
    def name(self) -> str:
        return "Sendblue"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Hermes gateway v0.18+ passes this keyword on both first connect and
        # reconnect attempts. The adapter does not need distinct behavior yet,
        # but it must honor BasePlatformAdapter's current contract.
        del is_reconnect
        missing = []
        if not self.settings.api_key:
            missing.append("SENDBLUE_API_KEY")
        if not self.settings.api_secret:
            missing.append("SENDBLUE_API_SECRET")
        if not self.settings.phone_number:
            missing.append("SENDBLUE_PHONE_NUMBER")
        if missing:
            msg = f"Missing Sendblue configuration: {', '.join(missing)}"
            logger.error(msg)
            self._set_fatal_error("config_missing", msg, retryable=False)
            return False

        if not self._acquire_platform_lock("sendblue", self.settings.phone_number, "Sendblue line"):
            return False

        try:
            if self.settings.webhook_enabled:
                if not _is_valid_webhook_secret(self.settings.webhook_secret):
                    msg = (
                        "Webhook mode requires SENDBLUE_WEBHOOK_SECRET with at least "
                        f"{MIN_WEBHOOK_SECRET_CHARS} non-placeholder characters"
                    )
                    logger.error(msg)
                    self._set_fatal_error("webhook_secret_missing", msg, retryable=False)
                    self._release_platform_lock()
                    return False

                self._webhook_server = SendblueWebhookServer(self, self.settings)
                self._webhook_server.start()
                logger.info(
                    "Sendblue webhook listening on http://%s:%s%s",
                    self.settings.webhook_host,
                    self.settings.webhook_port,
                    self.settings.webhook_path,
                )
            else:
                self._poll_task = asyncio.create_task(self._poll_loop(), name="sendblue-poll")
                logger.info("Sendblue polling every %.1fs", self.settings.poll_interval_seconds)

            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="sendblue-cleanup")
            self._mark_connected()
            return True
        except Exception as exc:
            self._release_platform_lock()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            logger.error("Sendblue connect failed: %s", exc, exc_info=True)
            return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

        for task in (self._poll_task, self._cleanup_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._poll_task = None
        self._cleanup_task = None

        if self._webhook_server:
            await asyncio.to_thread(self._webhook_server.stop)
            self._webhook_server = None

        self._release_platform_lock()
        self._store.close()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60 * 60)
            try:
                self._store.cleanup()
            except Exception:
                logger.debug("Sendblue cleanup failed", exc_info=True)

    async def _poll_loop(self) -> None:
        while True:
            started_at = datetime.now(timezone.utc)
            try:
                messages = await self.client.list_inbound_messages(self._last_poll_time)
                for message in messages:
                    await self._handle_sendblue_message(message)
                self._last_poll_time = started_at
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Sendblue poll failed: %s", exc)

            await asyncio.sleep(max(1.0, self.settings.poll_interval_seconds))

    async def _handle_sendblue_message(self, message: Dict[str, Any]) -> None:
        if message.get("is_outbound"):
            return

        message_id = str(message.get("message_handle") or message.get("id") or "").strip()
        from_number = _normalize_phone(str(message.get("from_number") or message.get("number") or ""))
        if not message_id or not from_number:
            logger.warning(
                "Sendblue inbound message missing message_handle or from_number; keys=%s",
                sorted(str(key) for key in message.keys()),
            )
            return

        recipient_numbers = {
            _normalize_phone(str(message.get(key) or ""))
            for key in ("to_number", "sendblue_number")
            if message.get(key)
        }
        if self.settings.phone_number and self.settings.phone_number not in recipient_numbers:
            logger.warning(
                "Dropping Sendblue message %s for unexpected recipient fields=%s",
                message_id[-8:],
                ",".join(sorted(_redacted_phone(num) for num in recipient_numbers)) or "missing",
            )
            return

        if not self._store.try_mark(message_id):
            return

        content = str(message.get("content") or "").strip()
        media_url = str(message.get("media_url") or "").strip()

        text = content
        media_urls: List[str] = []
        media_types: List[str] = []
        message_type = MessageType.TEXT

        if media_url:
            # Download + cache the attachment so the gateway can attach images to
            # the model's vision input and route voice notes to STT. On any failure
            # (download error, oversize, or an unclassifiable type) fall back to the
            # plain [Media: <url>] marker so the message is never lost.
            try:
                data, content_type = await self.client.download_media(media_url)
                filename = _normalize_upload_filename(
                    parse.unquote(os.path.basename(parse.urlparse(media_url).path))
                )
                cached = _classify_and_cache(data, filename, content_type)
                if cached is None:
                    raise RuntimeError(
                        f"unclassifiable media (content-type={content_type or 'unknown'})"
                    )
                cached_path, cached_mime, cached_kind = cached
                media_urls.append(cached_path)
                media_types.append(cached_mime)
                message_type = _KIND_TO_MESSAGE_TYPE.get(cached_kind, MessageType.DOCUMENT)
                if not content:
                    text = _KIND_TO_PLACEHOLDER.get(cached_kind, "(attachment)")
            except Exception as exc:
                logger.warning(
                    "Sendblue media handling failed for %s; falling back to link: %s",
                    message_id[-8:],
                    exc,
                )
                media_urls = []
                media_types = []
                message_type = MessageType.TEXT
                text = (
                    f"{content}\n\n[Media: {media_url}]" if content else f"[Media: {media_url}]"
                )

        if not text:
            return

        if self.settings.mark_read:
            try:
                await self.client.mark_read(from_number)
            except Exception:
                logger.debug("Sendblue mark-read failed", exc_info=True)

        source = self.build_source(
            chat_id=from_number,
            chat_name=f"Sendblue contact {_redacted_phone(from_number)}",
            chat_type="dm",
            user_id=from_number,
            user_name=f"Sendblue contact {_redacted_phone(from_number)}",
            message_id=message_id,
        )
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            media_urls=media_urls,
            media_types=media_types,
            raw_message={
                "message_handle": message_id,
                "service": message.get("service"),
                "message_type": message.get("message_type"),
                "has_media": bool(media_urls),
            },
            message_id=message_id,
            timestamp=_parse_datetime(message.get("date_sent") or message.get("date_created")),
        )
        await self.handle_message(event)

    def format_message(self, content: str) -> str:
        """Strip markdown — iMessage/SMS/RCS render it as literal characters.

        Mirrors the SMS, BlueBubbles, and Photon adapters: Sendblue is a
        plain-text transport, so **bold**, headings, [links](url), and code
        fences would otherwise reach the recipient as raw syntax.
        """
        return strip_markdown(content or "")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to, metadata
        target = _normalize_phone(chat_id)
        if not target:
            return SendResult(success=False, error="Missing destination phone number")

        text = self.format_message(content)
        if not text:
            return SendResult(success=False, error="Empty message")

        # Companion cadence: multi-bubble post-split + typing/thought pauses.
        # Falls back to length-only splits when chunked_replies is off.
        if self.settings.chunked_replies:
            beats = _split_chat_beats(text, max_beats=self.settings.chunk_max)
        else:
            beats = _split_message(text)

        message_ids: List[str] = []
        try:
            for index, beat in enumerate(beats):
                if index > 0:
                    # Thought-pause between bubbles: hold the iMessage ”…” for
                    # the full delay so it reads as "still typing", not a flash.
                    pause = _chunk_pause_seconds(
                        self.settings.chunk_delay_min_ms,
                        self.settings.chunk_delay_max_ms,
                    )
                    # Floor the pause a bit so typing is human-visible even when
                    # min_ms is set very low.
                    pause = max(pause, 0.65)
                    pause_ms = int(pause * 1000)
                    if self.settings.chunk_typing:
                        try:
                            await self.client.send_typing_indicator(
                                target,
                                state="start",
                                # Slight buffer so the indicator doesn't drop
                                # in the last milliseconds before send.
                                max_duration_ms=pause_ms + 600,
                            )
                        except Exception as typing_exc:
                            logger.warning(
                                "Sendblue inter-bubble typing failed for %s: %s",
                                _redacted_phone(target),
                                typing_exc,
                            )
                    await asyncio.sleep(pause)
                message_ids.append(await self.client.send_message(target, beat))
            if not message_ids:
                return SendResult(success=False, error="Empty message")
            if len(beats) > 1:
                logger.info(
                    "Sendblue sent %d chat beats to %s (typing=%s)",
                    len(beats),
                    _redacted_phone(target),
                    self.settings.chunk_typing,
                )
            return SendResult(success=True, message_id=",".join(message_ids))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        del metadata
        try:
            await self.client.send_typing_indicator(chat_id)
        except Exception:
            logger.debug("Sendblue typing indicator failed", exc_info=True)

    async def _attach_local_or_remote(
        self,
        chat_id: str,
        media: str,
        caption: Optional[str] = None,
        *,
        as_voice_note: bool = False,
    ) -> SendResult:
        """Upload a local file (or pass through a URL) and send it via Sendblue.

        When ``as_voice_note`` is True, local audio is converted to ``.caf`` so
        iMessage renders a native voice-note bubble (Sendblue docs).
        """
        target = _normalize_phone(chat_id)
        if not target:
            return SendResult(success=False, error="Missing destination phone number")
        if not media:
            return SendResult(success=False, error="Missing media path or URL")

        media_url = media
        if _is_local_media(media):
            try:
                media_url = await self.client.upload_file(
                    media, as_voice_note=as_voice_note
                )
                logger.info(
                    "Sendblue uploaded local file %s -> %s (voice_note=%s)",
                    media,
                    media_url,
                    as_voice_note,
                )
            except (FileNotFoundError, ValueError) as exc:
                return SendResult(success=False, error=str(exc), retryable=False)
            except Exception as exc:
                return SendResult(success=False, error=str(exc), retryable=True)

        try:
            # Voice notes are media-only; a text caption would force a separate
            # bubble and can prevent the native voice UI on some clients.
            body_text = "" if as_voice_note else self.format_message(caption or "")
            if as_voice_note and caption:
                # Send the voice note first, then caption text if present.
                message_id = await self.client.send_message(
                    target, "", media_url=media_url
                )
                cap = self.format_message(caption)
                if cap:
                    await self.client.send_message(target, cap)
                return SendResult(success=True, message_id=message_id)

            message_id = await self.client.send_message(
                target, body_text, media_url=media_url
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to, metadata
        return await self._attach_local_or_remote(chat_id, image_url, caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        return await self._attach_local_or_remote(chat_id, image_path, caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        del file_name, reply_to, metadata, kwargs
        return await self._attach_local_or_remote(chat_id, file_path, caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        return await self._attach_local_or_remote(chat_id, video_path, caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        # Force CAF so iMessage shows a native voice-note bubble, not a file.
        return await self._attach_local_or_remote(
            chat_id, audio_path, caption, as_voice_note=True
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        phone = _normalize_phone(chat_id)
        return {"name": phone, "type": "dm"}


def check_requirements() -> bool:
    """No optional dependencies are required; Hermes supplies the gateway API."""
    return True


def validate_config(config: Any) -> bool:
    settings = _settings_from_config(config)
    return bool(settings.api_key and settings.api_secret and settings.phone_number)


def is_connected(config: Any) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    settings = _settings_from_config(type("Config", (), {"extra": {}})())
    if not (settings.api_key and settings.api_secret and settings.phone_number):
        return None

    seed: Dict[str, Any] = {
        "api_key": settings.api_key,
        "api_secret": settings.api_secret,
        "phone_number": settings.phone_number,
        "api_base": settings.api_base,
        "poll_interval_seconds": settings.poll_interval_seconds,
    }

    home = _env_first("SENDBLUE_HOME_CHANNEL", "SENDBLUE_HOME_PHONE")
    if home:
        home_phone = _normalize_phone(home)
        seed["home_channel"] = {"chat_id": home_phone, "name": home_phone}

    if settings.webhook_enabled:
        seed["webhook"] = {
            "enabled": True,
            "host": settings.webhook_host,
            "port": settings.webhook_port,
            "path": settings.webhook_path,
            "secret": settings.webhook_secret,
        }

    return seed


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    del thread_id, force_document
    settings = _settings_from_config(pconfig)
    if not (settings.api_key and settings.api_secret and settings.phone_number):
        return {"error": "Sendblue standalone send: missing SENDBLUE_API_KEY, SENDBLUE_API_SECRET, or SENDBLUE_PHONE_NUMBER"}

    client = SendblueClient(settings)
    target = _normalize_phone(chat_id or _env_first("SENDBLUE_HOME_CHANNEL", "SENDBLUE_HOME_PHONE"))
    if not target:
        return {"error": "Sendblue standalone send: missing chat_id or SENDBLUE_HOME_CHANNEL"}

    try:
        ids: List[str] = []
        # Strip markdown like the interactive send() path — Sendblue is plain text.
        for chunk in _split_message(strip_markdown(message or "")):
            ids.append(await client.send_message(target, chunk))

        for media in media_files or []:
            media_str = str(media)
            if _is_local_media(media_str):
                try:
                    uploaded_url = await client.upload_file(media_str)
                except Exception as exc:
                    return {
                        "error": (
                            f"Sendblue standalone send failed to upload {media_str}: {exc}"
                        )
                    }
                ids.append(await client.send_message(target, "", media_url=uploaded_url))
            else:
                ids.append(await client.send_message(target, "", media_url=media_str))

        return {"success": True, "message_id": ",".join(ids) if ids else ""}
    except Exception as exc:
        return {"error": f"Sendblue standalone send failed: {exc}"}


def interactive_setup() -> None:
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
        save_env_value,
    )

    print_header("Sendblue")
    existing = get_env_value("SENDBLUE_PHONE_NUMBER")
    if existing:
        print_info(f"Sendblue is already configured for {existing}")
        if not prompt_yes_no("Reconfigure Sendblue?", False):
            return

    print_info("Sendblue lets Hermes talk over iMessage, SMS, and RCS.")
    api_key = prompt("Sendblue API key", default=get_env_value("SENDBLUE_API_KEY") or "", password=True)
    api_secret = prompt("Sendblue API secret", password=True)
    phone_number = prompt("Your Sendblue phone number", default=existing or "")

    if not (api_key and api_secret and phone_number):
        print_warning("API key, API secret, and phone number are required.")
        return

    save_env_value("SENDBLUE_API_KEY", api_key.strip())
    save_env_value("SENDBLUE_API_SECRET", api_secret.strip())
    save_env_value("SENDBLUE_PHONE_NUMBER", _normalize_phone(phone_number))

    allowed = prompt(
        "Allowed phone numbers (comma-separated, leave empty to use pairing)",
        default=get_env_value("SENDBLUE_ALLOWED_USERS") or "",
    )
    save_env_value("SENDBLUE_ALLOWED_USERS", ",".join(_normalize_phone(p) for p in allowed.split(",") if p.strip()))

    home = prompt(
        "Default delivery phone for cron/notifications (optional)",
        default=get_env_value("SENDBLUE_HOME_CHANNEL") or "",
    )
    if home:
        save_env_value("SENDBLUE_HOME_CHANNEL", _normalize_phone(home))

    if prompt_yes_no("Enable webhook mode? (requires a public HTTPS tunnel/proxy)", False):
        secret = prompt(f"Webhook secret (minimum {MIN_WEBHOOK_SECRET_CHARS} chars)", password=True)
        if not _is_valid_webhook_secret(secret):
            print_warning(
                f"Webhook secret must be at least {MIN_WEBHOOK_SECRET_CHARS} chars and cannot be 'change-me'. "
                "Webhook mode was not enabled."
            )
            save_env_value("SENDBLUE_WEBHOOK_ENABLED", "false")
            print_success("Sendblue configuration saved to ~/.hermes/.env")
            print_info("Restart the gateway for changes to take effect: hermes gateway restart")
            return

        save_env_value("SENDBLUE_WEBHOOK_ENABLED", "true")
        port = prompt("Local webhook port", default=get_env_value("SENDBLUE_WEBHOOK_PORT") or "3141")
        path = prompt("Webhook path", default=get_env_value("SENDBLUE_WEBHOOK_PATH") or "/webhook/sendblue")
        save_env_value("SENDBLUE_WEBHOOK_PORT", port or "3141")
        save_env_value("SENDBLUE_WEBHOOK_PATH", path or "/webhook/sendblue")
        save_env_value("SENDBLUE_WEBHOOK_SECRET", secret)
    else:
        save_env_value("SENDBLUE_WEBHOOK_ENABLED", "false")

    print_success("Sendblue configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="sendblue",
        label="Sendblue",
        adapter_factory=lambda cfg: SendblueAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["SENDBLUE_API_KEY", "SENDBLUE_API_SECRET", "SENDBLUE_PHONE_NUMBER"],
        install_hint="Install this plugin under ~/.hermes/plugins/sendblue and set Sendblue credentials",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="SENDBLUE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="SENDBLUE_ALLOWED_USERS",
        allow_all_env="SENDBLUE_ALLOW_ALL_USERS",
        max_message_length=MAX_SENDBLUE_BODY_CHARS,
        pii_safe=True,
        emoji="SB",
        allow_update_command=True,
        platform_hint=(
            "You are chatting through Sendblue over iMessage/SMS/RCS. "
            "Phone screens are small, so keep replies concise unless the user asks for detail. "
            "Sendblue delivers images, videos, audio, and documents as native iMessage/SMS attachments — "
            "local file paths in MEDIA:<path> tags are uploaded automatically."
        ),
    )
