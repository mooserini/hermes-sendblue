"""Unit tests for the local file upload helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_adapter():
    """Load adapter.py as a standalone module without hermes_cli/gateway installed."""
    # adapter.py imports from `gateway.*`. Prefer the real hermes-agent core when
    # it's importable (e.g. running inside a hermes-agent checkout); otherwise stub
    # gateway.* so this test still runs anywhere.
    try:
        import gateway.platforms.base  # noqa: F401

        _real_core = True
    except Exception:
        _real_core = False

    if not _real_core and "gateway" not in sys.modules:
        gateway_mod = type(sys)("gateway")
        config_mod = type(sys)("gateway.config")
        config_mod.Platform = lambda name: name  # type: ignore[attr-defined]
        platforms_mod = type(sys)("gateway.platforms")
        base_mod = type(sys)("gateway.platforms.base")

        class _StubAdapter:
            def __init__(self, *args, **kwargs):
                pass

            def _acquire_platform_lock(self, *args, **kwargs):
                return True

            def _release_platform_lock(self, *args, **kwargs):
                pass

            def _set_fatal_error(self, *args, **kwargs):
                pass

            def _mark_connected(self):
                pass

            def _mark_disconnected(self):
                pass

            def build_source(self, **kwargs):
                return kwargs

            async def handle_message(self, event):
                pass

        base_mod.BasePlatformAdapter = _StubAdapter  # type: ignore[attr-defined]
        base_mod.MessageEvent = SimpleNamespace  # type: ignore[attr-defined]

        class _MessageType:
            TEXT = "text"
            PHOTO = "photo"
            VIDEO = "video"
            AUDIO = "audio"
            VOICE = "voice"
            DOCUMENT = "document"

        base_mod.MessageType = _MessageType  # type: ignore[attr-defined]

        class _SendResult(SimpleNamespace):
            pass

        base_mod.SendResult = _SendResult  # type: ignore[attr-defined]
        base_mod.cache_image_from_bytes = lambda *a, **k: "/tmp/fake-image"  # type: ignore[attr-defined]
        base_mod.cache_audio_from_bytes = lambda *a, **k: "/tmp/fake-audio"  # type: ignore[attr-defined]
        base_mod.cache_video_from_bytes = lambda *a, **k: "/tmp/fake-video"  # type: ignore[attr-defined]
        base_mod.cache_document_from_bytes = lambda *a, **k: "/tmp/fake-doc"  # type: ignore[attr-defined]

        sys.modules["gateway"] = gateway_mod
        sys.modules["gateway.config"] = config_mod
        sys.modules["gateway.platforms"] = platforms_mod
        sys.modules["gateway.platforms.base"] = base_mod

    spec = importlib.util.spec_from_file_location("hermes_sendblue_adapter", ROOT / "adapter.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_sendblue_adapter"] = module
    spec.loader.exec_module(module)
    return module


adapter = _load_adapter()


class IsLocalMediaTests(unittest.TestCase):
    def test_http_url_is_not_local(self) -> None:
        self.assertFalse(adapter._is_local_media("http://example.com/x.png"))

    def test_https_url_is_not_local(self) -> None:
        self.assertFalse(adapter._is_local_media("https://cdn.example.com/x.png"))

    def test_absolute_path_is_local(self) -> None:
        self.assertTrue(adapter._is_local_media("/tmp/foo.png"))

    def test_relative_path_is_local(self) -> None:
        self.assertTrue(adapter._is_local_media("./bar.pdf"))

    def test_empty_is_not_local(self) -> None:
        self.assertFalse(adapter._is_local_media(""))

    def test_none_is_not_local(self) -> None:
        self.assertFalse(adapter._is_local_media(None))


class NormalizeUploadFilenameTests(unittest.TestCase):
    def test_lowercases_extension(self) -> None:
        self.assertEqual(adapter._normalize_upload_filename("Photo.JPG"), "Photo.jpg")
        self.assertEqual(adapter._normalize_upload_filename("doc.PDF"), "doc.pdf")
        self.assertEqual(adapter._normalize_upload_filename("Audio Message.CAF"), "Audio Message.caf")

    def test_preserves_already_lowercase_extension(self) -> None:
        self.assertEqual(adapter._normalize_upload_filename("shot.png"), "shot.png")

    def test_strips_directory_components(self) -> None:
        self.assertEqual(
            adapter._normalize_upload_filename("/tmp/out/My Photo.PNG"),
            "My Photo.png",
        )

    def test_empty_falls_back_to_file(self) -> None:
        self.assertEqual(adapter._normalize_upload_filename(""), "file")

    def test_strips_quotes_and_newlines(self) -> None:
        self.assertEqual(
            adapter._normalize_upload_filename('weird"\nname.JPG'),
            "weirdname.jpg",
        )


class InboundCafTranscodeTests(unittest.TestCase):
    def test_real_caf_bytes_become_m4a_when_ffmpeg_available(self) -> None:
        if not adapter.shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")
        sample = Path.home() / ".hermes" / "audio_cache" / "audio_fe1ea53f3c45.caf"
        if not sample.is_file():
            self.skipTest("no local CAF sample in audio_cache")
        out = adapter._transcode_caf_bytes_to_m4a(sample.read_bytes())
        self.assertIsNotNone(out)
        assert out is not None
        data, ext = out
        self.assertEqual(ext, ".m4a")
        self.assertGreater(len(data), 100)
        # ISO BMFF / M4A magic is typically at offset 4: 'ftyp'
        self.assertIn(b"ftyp", data[:32])


class OutboundVoiceNoteTests(unittest.TestCase):
    def test_voice_note_transcodes_mp3_to_caf_when_ffmpeg_available(self) -> None:
        if not adapter.shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")
        samples = sorted(
            (Path.home() / ".hermes" / "audio_cache").glob("tts_*.mp3"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not samples:
            self.skipTest("no local TTS mp3 sample")
        read_path, upload_name, cleanup = adapter._prepare_local_media_for_upload(
            samples[0], as_voice_note=True
        )
        try:
            self.assertEqual(upload_name, "Audio Message.caf")
            self.assertTrue(str(read_path).endswith(".caf"))
            self.assertTrue(read_path.is_file())
            self.assertGreater(read_path.stat().st_size, 100)
            # CoreAudio Format magic
            header = read_path.read_bytes()[:4]
            self.assertEqual(header, b"caff")
        finally:
            adapter._cleanup_prepared_media(cleanup)

    def test_non_voice_upload_does_not_force_caf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "clip.mp3"
            src.write_bytes(b"not-real-mp3")
            read_path, upload_name, cleanup = adapter._prepare_local_media_for_upload(
                src, as_voice_note=False
            )
            self.assertEqual(read_path, src)
            self.assertEqual(upload_name, "clip.mp3")
            self.assertIsNone(cleanup)


class PrepareLocalMediaTests(unittest.TestCase):
    def test_uppercase_extension_keeps_source_path_and_lowercases_name(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Holiday.JPG"
            src.write_bytes(b"fake-jpeg-bytes")
            read_path, upload_name, cleanup = adapter._prepare_local_media_for_upload(src)
            self.assertEqual(read_path, src)
            self.assertEqual(upload_name, "Holiday.jpg")
            self.assertIsNone(cleanup)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            adapter._prepare_local_media_for_upload("/no/such/file.JPG")

    def test_upload_file_uses_lowercased_multipart_filename(self) -> None:
        import tempfile

        client = adapter.SendblueClient(
            adapter.SendblueSettings(
                api_key="k",
                api_secret="s",
                phone_number="+15550000000",
            )
        )
        captured: dict = {}

        def fake_post(path, headers, body, *, timeout=60.0):
            captured["body"] = body
            return {"status": "OK", "media_url": "https://cdn.sendblue.test/up"}

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Scan.PDF"
            src.write_bytes(b"%PDF-1.4")
            with patch.object(client, "_raw_post_sync", side_effect=fake_post):
                url = asyncio.run(client.upload_file(str(src)))

        self.assertEqual(url, "https://cdn.sendblue.test/up")
        self.assertIn(b'filename="Scan.pdf"', captured["body"])
        self.assertNotIn(b'filename="Scan.PDF"', captured["body"])


class BuildMultipartTests(unittest.TestCase):
    def test_includes_boundary_header_and_footer(self) -> None:
        body = adapter._build_multipart("hello.png", b"\x89PNGdata", "BOUND123")
        self.assertIn(b"--BOUND123\r\n", body)
        self.assertTrue(body.endswith(b"\r\n--BOUND123--\r\n"))

    def test_includes_form_field_name_and_filename(self) -> None:
        body = adapter._build_multipart("doc.pdf", b"PDFBYTES", "BX")
        self.assertIn(b'name="file"', body)
        self.assertIn(b'filename="doc.pdf"', body)

    def test_uses_octet_stream_content_type(self) -> None:
        body = adapter._build_multipart("x", b"y", "BX")
        self.assertIn(b"Content-Type: application/octet-stream", body)

    def test_payload_bytes_are_embedded(self) -> None:
        payload = b"\x00\x01\x02RAW\xff"
        body = adapter._build_multipart("any.bin", payload, "BX")
        self.assertIn(payload, body)

    def test_filename_strips_quotes_and_newlines(self) -> None:
        body = adapter._build_multipart('weird"\nname.png', b"x", "BX")
        # Sanitized name should appear in the header.
        self.assertIn(b'filename="weirdname.png"', body)


class UploadGuardTests(unittest.TestCase):
    def _client(self) -> "adapter.SendblueClient":
        settings = adapter.SendblueSettings(
            api_key="k",
            api_secret="s",
            phone_number="+15550000000",
        )
        return adapter.SendblueClient(settings)

    def test_empty_bytes_rejected(self) -> None:
        client = self._client()
        with self.assertRaises(ValueError):
            asyncio.run(client.upload_file_from_bytes("empty.png", b""))

    def test_oversize_bytes_rejected(self) -> None:
        client = self._client()
        oversize = b"\x00" * (adapter.SENDBLUE_MAX_UPLOAD_BYTES + 1)
        with self.assertRaises(ValueError):
            asyncio.run(client.upload_file_from_bytes("big.bin", oversize))

    def test_missing_file_raises_filenotfound(self) -> None:
        client = self._client()
        with self.assertRaises(FileNotFoundError):
            asyncio.run(client.upload_file("/nonexistent/path/that/does/not/exist.png"))

    def test_upload_posts_to_expected_endpoint(self) -> None:
        client = self._client()
        captured: dict = {}

        def fake_post(path, headers, body, *, timeout=60.0):
            captured["path"] = path
            captured["headers"] = headers
            captured["body"] = body
            return {"status": "OK", "media_url": "https://cdn.sendblue.test/abc"}

        with patch.object(client, "_raw_post_sync", side_effect=fake_post):
            url = asyncio.run(client.upload_file_from_bytes("hi.png", b"PNGBYTES"))

        self.assertEqual(url, "https://cdn.sendblue.test/abc")
        self.assertEqual(captured["path"], "/api/upload-file")
        self.assertIn("multipart/form-data; boundary=", captured["headers"]["Content-Type"])
        self.assertIn("sb-api-key-id", captured["headers"])
        self.assertIn(b'name="file"', captured["body"])
        self.assertIn(b"PNGBYTES", captured["body"])

    def test_upload_raises_when_response_missing_media_url(self) -> None:
        client = self._client()

        def fake_post(path, headers, body, *, timeout=60.0):
            return {"status": "OK"}

        with patch.object(client, "_raw_post_sync", side_effect=fake_post):
            with self.assertRaises(RuntimeError):
                asyncio.run(client.upload_file_from_bytes("x.png", b"data"))


class FormatMessageTests(unittest.TestCase):
    """Outbound markdown stripping for the plain-text iMessage/SMS/RCS transport.

    Mirrors the SMS/BlueBubbles/Photon convention (``format_message`` ->
    ``strip_markdown``). Runs against whichever ``strip_markdown`` the adapter
    loaded — the real core helper when present, else the local fallback, which
    is regex-for-regex identical.
    """

    @staticmethod
    def _fmt(text: str) -> str:
        return adapter.SendblueAdapter.format_message(SimpleNamespace(), text)

    def test_bold_and_italic_unwrapped(self) -> None:
        self.assertEqual(self._fmt("**bold** and *italic*"), "bold and italic")

    def test_headings_stripped(self) -> None:
        self.assertEqual(self._fmt("# Title\nbody"), "Title\nbody")

    def test_inline_code_unwrapped(self) -> None:
        self.assertEqual(self._fmt("run `make test` now"), "run make test now")

    def test_code_fence_removed_but_body_kept(self) -> None:
        out = self._fmt("```python\nprint(1)\n```")
        self.assertIn("print(1)", out)
        self.assertNotIn("```", out)

    def test_markdown_link_collapses_to_anchor_text(self) -> None:
        # Documented lossy case shared with SMS/BlueBubbles/Photon: the URL drops.
        self.assertEqual(self._fmt("see [the docs](https://x.com/a)"), "see the docs")

    def test_raw_url_is_preserved(self) -> None:
        self.assertEqual(self._fmt("see https://x.com/a"), "see https://x.com/a")

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(self._fmt("just a normal message"), "just a normal message")

    def test_empty_is_safe(self) -> None:
        self.assertEqual(self._fmt(""), "")


if __name__ == "__main__":
    unittest.main()
