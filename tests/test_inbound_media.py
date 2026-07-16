"""Unit tests for inbound media download + classification.

These exercise the REAL ``gateway.platforms.base`` media-cache helpers, so they
require the hermes-agent core on ``sys.path`` (i.e. running inside a hermes-agent
checkout, ideally with this plugin vendored at ``plugins/platforms/sendblue``).
When the real core is not importable they skip cleanly — the lightweight stub in
``test_upload.py`` only provides ``MessageType.TEXT`` and cannot classify media.

Written as unittest (matching this repo's other tests) so they run under both
``python -m unittest`` and pytest, and skip rather than error without core.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _real_core_available() -> bool:
    try:
        import gateway.platforms.base as base  # noqa: F401
    except Exception:
        return False
    # cache_image_from_url exists only on the real core — neither the adapter nor the
    # test_upload stub imports/defines it, so it's a reliable real-vs-stub sentinel.
    return hasattr(base, "cache_image_from_bytes") and hasattr(base, "cache_image_from_url")


_REAL_CORE = _real_core_available()

adapter = None
if _REAL_CORE:
    _spec = importlib.util.spec_from_file_location(
        "hermes_sendblue_adapter_media", ROOT / "adapter.py"
    )
    assert _spec and _spec.loader
    adapter = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_sendblue_adapter_media"] = adapter
    _spec.loader.exec_module(adapter)

# A valid PNG magic header — enough for cache_image_from_bytes()'s magic-byte check.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@unittest.skipUnless(
    _REAL_CORE, "requires the real hermes-agent core (run inside a hermes-agent checkout)"
)
class InboundMediaTests(unittest.TestCase):
    @staticmethod
    def _downloader(data: bytes, content_type: str):
        async def _dl(url, *, authenticated=False):
            return data, content_type

        return _dl

    def _run(self, download, **overrides):
        """Drive _handle_sendblue_message with a duck-typed self; return events."""
        settings = adapter.SendblueSettings(
            api_key="k", api_secret="s", phone_number="+15550000000"
        )
        events: list = []

        async def handle_message(event):
            events.append(event)

        fake = SimpleNamespace(
            settings=settings,
            client=SimpleNamespace(download_media=download),
            _store=SimpleNamespace(try_mark=lambda mid: True),
            build_source=lambda **kw: SimpleNamespace(**kw),
            handle_message=handle_message,
        )
        msg = {
            "message_handle": "m-1",
            "from_number": "+15551112222",
            "to_number": "+15550000000",
            "sendblue_number": "+15550000000",
            "is_outbound": False,
            "content": "",
            "media_url": "https://cdn.sendblue.test/x.png",
            "date_sent": "2026-06-12T00:00:00Z",
        }
        msg.update(overrides)
        asyncio.run(adapter.SendblueAdapter._handle_sendblue_message(fake, msg))
        return events

    def test_png_image_becomes_photo(self):
        ev = self._run(
            self._downloader(_PNG_BYTES, "image/png"),
            media_url="https://cdn.sendblue.test/photo.png",
        )
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].message_type, adapter.MessageType.PHOTO)
        self.assertEqual(len(ev[0].media_urls), 1)
        self.assertEqual(ev[0].media_types, ["image/png"])

    def test_caf_voice_becomes_voice(self):
        # Prefer a real CAF sample when one exists (ffmpeg path); otherwise fall
        # back to raw bytes which still classify as VOICE without STT.
        sample = Path.home() / ".hermes" / "audio_cache" / "audio_fe1ea53f3c45.caf"
        if sample.is_file() and sample.stat().st_size > 0:
            caf_bytes = sample.read_bytes()
        else:
            caf_bytes = b"caf-bytes-not-validated"

        ev = self._run(
            self._downloader(caf_bytes, "audio/x-caf"),
            media_url="https://cdn.sendblue.test/voice.caf",
        )
        self.assertEqual(ev[0].message_type, adapter.MessageType.VOICE)
        # When ffmpeg can transcode, inbound CAF is cached as M4A for STT.
        if ev[0].media_types == ["audio/mp4"]:
            self.assertTrue(ev[0].media_urls[0].endswith(".m4a"))
        else:
            self.assertEqual(ev[0].media_types, ["audio/x-caf"])

    def test_mp4_becomes_video(self):
        ev = self._run(
            self._downloader(b"video-bytes", "video/mp4"),
            media_url="https://cdn.sendblue.test/clip.mp4",
        )
        self.assertEqual(ev[0].message_type, adapter.MessageType.VIDEO)

    def test_pdf_becomes_document(self):
        ev = self._run(
            self._downloader(b"%PDF-1.4 fake pdf bytes", "application/pdf"),
            media_url="https://cdn.sendblue.test/doc.pdf",
        )
        self.assertEqual(ev[0].message_type, adapter.MessageType.DOCUMENT)
        self.assertEqual(ev[0].media_types, ["application/pdf"])

    def test_unclassifiable_falls_back_to_marker(self):
        ev = self._run(
            self._downloader(b"\x00\x01\x02", "application/octet-stream"),
            media_url="https://cdn.sendblue.test/download?id=abc",
        )
        self.assertEqual(ev[0].media_urls, [])
        self.assertEqual(ev[0].message_type, adapter.MessageType.TEXT)
        self.assertIn("[Media: ", ev[0].text)

    def test_empty_caption_image_not_dropped(self):
        ev = self._run(
            self._downloader(_PNG_BYTES, "image/png"),
            content="",
            media_url="https://cdn.sendblue.test/photo.png",
        )
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].text, "(image)")
        self.assertTrue(ev[0].media_urls)

    def test_download_failure_falls_back(self):
        async def boom(url, *, authenticated=False):
            raise RuntimeError("network down")

        with self.assertLogs("gateway.platforms.sendblue", level="WARNING") as cm:
            ev = self._run(boom, content="hi", media_url="https://cdn.sendblue.test/x.png")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].media_urls, [])
        self.assertIn("[Media: ", ev[0].text)
        self.assertTrue(any("media handling failed" in m.lower() for m in cm.output))

    def test_caption_and_media_preserved(self):
        ev = self._run(
            self._downloader(_PNG_BYTES, "image/png"),
            content="check this",
            media_url="https://cdn.sendblue.test/photo.png",
        )
        self.assertEqual(ev[0].text, "check this")
        self.assertTrue(ev[0].media_urls)


if __name__ == "__main__":
    unittest.main()
