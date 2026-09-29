"""
Unit tests for the T8 image pipeline, its capacity limits, and the game lock helpers.

Images are built in memory with Pillow. No database, Redis or running stack: the game lock is
tested against a mocked session and by compiling its SQL for MySQL.

Run with:
    cd tests/unit && PYTHONPATH=../../backend pytest test_image_service.py -v
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import struct
import threading
import zlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from PIL import GifImagePlugin, ImageCms, ImageFile, PngImagePlugin
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

from app.common.exceptions import (
    BuzzerError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnprocessableError,
    UnsupportedMediaTypeError,
)
from app.models import Game, Image
from app.services import auth_service, image_service
from app.services.game_lock import LOCK_CONFLICT_MESSAGE, fresh, lock_game
from app.services.image_service import BundleEntry, ProcessedImage

# ---------------------------------------------------------------------------
# Image factories
# ---------------------------------------------------------------------------

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def encode(img: PILImage.Image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, fmt, **options)
    return buffer.getvalue()


def solid(size=(40, 20), color="red", mode="RGB") -> PILImage.Image:
    return PILImage.new(mode, size, color)


def noise(size=(256, 256), seed=1) -> PILImage.Image:
    rng = random.Random(seed)
    return PILImage.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))


def png_header(width: int, height: int) -> bytes:
    """A PNG with a valid header declaring width × height, and no pixel data."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def exif_with_orientation(orientation: int) -> PILImage.Exif:
    exif = PILImage.Exif()
    exif[0x0112] = orientation
    return exif


def with_app13(jpeg: bytes) -> bytes:
    """Insert an IPTC/Photoshop APP13 segment right after the JPEG's SOI marker."""
    payload = (
        b"Photoshop 3.0\x00" + b"8BIM\x04\x04\x00\x00" + struct.pack(">I", 4) + b"abcd"
    )
    segment = b"\xff\xed" + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + segment + jpeg[2:]


def png_with_text(img: PILImage.Image, key="Software", value="Paint") -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    return encode(img, "PNG", pnginfo=info)


def animated(fmt: str, colors=("red", "blue", "green"), size=(30, 20)) -> bytes:
    frames = [solid(size, color) for color in colors]
    if fmt == "GIF":
        frames = [frame.convert("P") for frame in frames]
    return encode(frames[0], fmt, save_all=True, append_images=frames[1:], duration=100)


def opened(data: bytes) -> PILImage.Image:
    img = PILImage.open(io.BytesIO(data))
    img.load()
    return img


# ---------------------------------------------------------------------------
# Conformance: one test per condition (spec §2.5 step 4)
# ---------------------------------------------------------------------------


def conforms(data: bytes) -> bool:
    return image_service.conforms(opened(data), data)


class TestConformance:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_plain_images_conform(self, fmt):
        assert conforms(encode(solid(), fmt))

    def test_gif_never_conforms(self):
        assert not conforms(encode(solid().convert("P"), "GIF"))

    @pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
    def test_animated_images_dont_conform(self, fmt):
        assert opened(animated(fmt)).is_animated
        assert not conforms(animated(fmt))

    @pytest.mark.parametrize(
        ("size", "expected"),
        [((1600, 1), True), ((1, 1600), True), ((1601, 1), False), ((1, 1601), False)],
    )
    def test_longest_side_within_the_full_image_edge(self, size, expected):
        assert conforms(encode(solid(size), "PNG")) is expected

    @pytest.mark.parametrize(
        "make",
        [
            lambda: encode(solid(), "JPEG", exif=exif_with_orientation(1)),
            lambda: encode(solid(), "JPEG", xmp=b"<x:xmpmeta/>"),
            lambda: with_app13(encode(solid(), "JPEG")),
            lambda: encode(solid(), "JPEG", comment=b"note"),
            lambda: png_with_text(solid()),
            lambda: png_with_text(solid(), "XML:com.adobe.xmp", "<x:xmpmeta/>"),
            lambda: encode(solid(), "PNG", exif=exif_with_orientation(1)),
            lambda: encode(solid(), "WEBP", exif=exif_with_orientation(1)),
            lambda: encode(solid(), "WEBP", xmp=b"<x:xmpmeta/>"),
        ],
        ids=[
            "jpeg-exif",
            "jpeg-xmp",
            "jpeg-app13",
            "jpeg-comment",
            "png-text",
            "png-xmp",
            "png-exif",
            "webp-exif",
            "webp-xmp",
        ],
    )
    def test_metadata_prevents_conformance(self, make):
        assert not conforms(make())

    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_an_icc_profile_is_allowed(self, fmt):
        data = encode(solid(), fmt, icc_profile=SRGB_ICC)
        assert opened(data).info.get("icc_profile") == SRGB_ICC
        assert conforms(data)

    def test_length_within_the_full_image_size(self, monkeypatch):
        data = encode(solid(), "PNG")
        monkeypatch.setattr(image_service, "FULL_MAX_BYTES", len(data))
        assert conforms(data)
        monkeypatch.setattr(image_service, "FULL_MAX_BYTES", len(data) - 1)
        assert not conforms(data)


# ---------------------------------------------------------------------------
# The pipeline: conforming bytes, each normalization branch, the small copy
# ---------------------------------------------------------------------------


class TestPipeline:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_conforming_bytes_are_stored_unchanged(self, fmt):
        raw = encode(noise((300, 200)), fmt)
        result = image_service.process_image(raw)
        assert result.data == raw
        assert result.content_type == image_service.CONTENT_TYPES[fmt]

    def test_derived_fields_come_from_the_stored_bytes(self):
        result = image_service.process_image(encode(solid((3200, 400)), "PNG"))
        assert (result.width, result.height) == (1600, 200)
        assert opened(result.data).size == (1600, 200)
        assert result.byte_size == len(result.data)
        assert result.sha256 == hashlib.sha256(result.data).hexdigest()

    def test_exif_orientation_is_applied_and_removed(self):
        raw = encode(solid((40, 20)), "JPEG", exif=exif_with_orientation(6))
        result = image_service.process_image(raw)
        assert (result.width, result.height) == (20, 40)
        stored = opened(result.data)
        assert stored.format == "JPEG"
        assert "exif" not in stored.info
        assert image_service.conforms(stored, result.data)

    def test_jpeg_comment_normalizes_to_conforming_bytes(self):
        # Spec §4.15: Pillow's JPEG encoder writes info["comment"] back unless it is cleared.
        result = image_service.process_image(encode(solid(), "JPEG", comment=b"note"))
        stored = opened(result.data)
        assert "comment" not in stored.info
        assert image_service.conforms(stored, result.data)

    @pytest.mark.parametrize(
        ("raw", "fmt"),
        [
            (encode(solid(), "JPEG", xmp=b"<x:xmpmeta/>"), "JPEG"),
            (with_app13(encode(solid(), "JPEG")), "JPEG"),
            (png_with_text(solid()), "PNG"),
            (encode(solid(), "WEBP", exif=exif_with_orientation(1)), "WEBP"),
        ],
        ids=["jpeg-xmp", "jpeg-app13", "png-text", "webp-exif"],
    )
    def test_metadata_is_dropped_keeping_the_format(self, raw, fmt):
        result = image_service.process_image(raw)
        stored = opened(result.data)
        assert stored.format == fmt
        assert image_service.conforms(stored, result.data)

    def test_icc_profile_is_carried_into_reencoded_output(self):
        raw = encode(solid(), "JPEG", icc_profile=SRGB_ICC, comment=b"x")
        result = image_service.process_image(raw)
        assert result.data != raw
        assert opened(result.data).info.get("icc_profile") == SRGB_ICC
        assert opened(result.small_data).info.get("icc_profile") == SRGB_ICC

    def test_icc_profile_is_dropped_when_pixels_change_colour_space(self):
        cmyk = solid(mode="CMYK", color=(0, 255, 255, 0))
        raw = encode(cmyk, "JPEG", icc_profile=SRGB_ICC, comment=b"x")
        result = image_service.process_image(raw)
        full = opened(result.data)
        assert full.mode == "CMYK" and full.info.get("icc_profile") == SRGB_ICC
        small = opened(result.small_data)
        assert small.mode == "RGB" and "icc_profile" not in small.info

    def test_gif_becomes_a_png_of_its_first_frame(self):
        result = image_service.process_image(animated("GIF"))
        stored = opened(result.data)
        assert stored.format == "PNG"
        assert not getattr(stored, "is_animated", False)
        assert stored.convert("RGB").getpixel((0, 0)) == (255, 0, 0)

    @pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
    def test_animation_keeps_only_the_first_frame(self, fmt):
        result = image_service.process_image(animated(fmt))
        stored = opened(result.data)
        assert stored.format == fmt
        assert not stored.is_animated
        red, green, blue = stored.convert("RGB").getpixel((15, 10))
        assert red > 200 and green < 60 and blue < 60

    def test_large_images_are_scaled_down_never_up(self):
        assert (
            image_service.process_image(
                encode(solid((3200, 800)), "PNG", icc_profile=None)
            ).width
            == 1600
        )
        tall = image_service.process_image(png_with_text(solid((10, 2000))))
        assert (tall.width, tall.height) == (8, 1600)
        small = image_service.process_image(png_with_text(solid((10, 20))))
        assert (small.width, small.height) == (10, 20)

    def test_palette_images_are_resampled_not_nearest_neighbour(self):
        # Alternating black and white columns: a filtered 2:1 downscale is grey throughout;
        # Pillow's silent nearest-neighbour fallback for "P" would keep pure black or white.
        stripes = PILImage.new("P", (3200, 8))
        stripes.putpalette([0, 0, 0, 255, 255, 255])
        stripes.putdata([x % 2 for _ in range(8) for x in range(3200)])
        result = image_service.process_image(encode(stripes, "PNG"))
        stored = opened(result.data).convert("L")
        assert stored.size == (1600, 4)
        assert all(40 < value < 215 for value in stored.get_flattened_data())

    def test_16_bit_greyscale_is_scaled_not_clipped(self):
        raw = encode(PILImage.new("I;16", (8, 8), 32768), "PNG")
        result = image_service.process_image(raw)
        assert result.data == raw  # conforming, stored unchanged
        red, _, _ = opened(result.small_data).convert("RGB").getpixel((4, 4))
        assert 120 <= red <= 136  # clipping would make it 255

    def test_webp_fallback_when_the_native_encoding_is_too_large(self, monkeypatch):
        img = noise((256, 256))
        png_size = len(encode(img, "PNG"))
        webp_size = len(encode(img, "WEBP", quality=image_service.WEBP_QUALITY))
        assert webp_size < png_size
        monkeypatch.setattr(
            image_service, "FULL_MAX_BYTES", (png_size + webp_size) // 2
        )
        result = image_service.process_image(png_with_text(img))
        assert result.content_type == "image/webp"
        assert result.byte_size <= image_service.FULL_MAX_BYTES

    def test_too_large_even_as_webp_is_413(self, monkeypatch):
        monkeypatch.setattr(image_service, "FULL_MAX_BYTES", 100)
        with pytest.raises(PayloadTooLargeError):
            image_service.process_image(png_with_text(noise((64, 64))))

    def test_other_nonconformance_after_normalizing_is_a_server_error(
        self, monkeypatch
    ):
        # Without clearing `info`, the JPEG comment survives: a pipeline bug must not become
        # a client's "too large" (spec §2.5 step 5).
        monkeypatch.setattr(image_service, "_strip_info", lambda img: None)
        with pytest.raises(RuntimeError, match="metadata"):
            image_service.process_image(encode(solid(), "JPEG", comment=b"note"))


class TestSmallCopy:
    def test_always_webp_within_the_edge(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            result = image_service.process_image(encode(solid((1600, 800)), fmt))
            small = opened(result.small_data)
            assert small.format == "WEBP"
            assert small.size == (400, 200)

    def test_never_scaled_up(self):
        result = image_service.process_image(encode(solid((30, 10)), "PNG"))
        assert opened(result.small_data).size == (30, 10)

    def test_transparency_is_kept(self):
        img = PILImage.new("RGBA", (800, 800), (255, 0, 0, 255))
        img.paste((0, 0, 0, 0), (0, 0, 400, 400))
        small = opened(image_service.process_image(encode(img, "PNG")).small_data)
        assert small.mode == "RGBA"
        assert small.getpixel((10, 10))[3] == 0
        assert small.getpixel((390, 390))[3] == 255

    def test_gif_transparency_is_kept(self):
        gif = PILImage.new("P", (20, 20), 1)
        gif.putpalette([0, 0, 0, 255, 0, 0])
        gif.paste(0, (0, 0, 10, 10))
        result = image_service.process_image(encode(gif, "GIF", transparency=0))
        small = opened(result.small_data)
        assert small.getpixel((2, 2))[3] == 0
        assert small.getpixel((15, 15))[3] == 255

    def test_quality_fallback_then_413(self, monkeypatch):
        calls: list[tuple[str, int | None, int]] = []
        real_encode = image_service._encode

        def spy(img, fmt, **options):
            data = real_encode(img, fmt, **options)
            calls.append((fmt, options["quality"], len(data)))
            return data

        monkeypatch.setattr(image_service, "_encode", spy)
        raw = encode(
            noise((400, 400)), "PNG"
        )  # conforming: only the small copy encodes

        image_service.process_image(raw)
        [(_, quality, size_85)] = calls
        assert quality == image_service.WEBP_QUALITY

        calls.clear()
        monkeypatch.setattr(image_service, "SMALL_MAX_BYTES", size_85 - 1)
        result = image_service.process_image(raw)
        assert [(fmt, quality) for fmt, quality, _ in calls] == [
            ("WEBP", image_service.WEBP_QUALITY),
            ("WEBP", image_service.SMALL_FALLBACK_QUALITY),
        ]
        size_50 = calls[1][2]
        assert size_50 < size_85
        assert len(result.small_data) == size_50

        monkeypatch.setattr(image_service, "SMALL_MAX_BYTES", size_50 - 1)
        with pytest.raises(PayloadTooLargeError):
            image_service.process_image(raw)


class TestIdempotence:
    @pytest.mark.parametrize(
        "raw",
        [
            encode(solid((40, 20)), "JPEG", exif=exif_with_orientation(6)),
            encode(noise((2400, 300)), "JPEG", comment=b"x"),
            animated("GIF"),
            animated("WEBP"),
            animated("PNG"),
            png_with_text(solid((3000, 50))),
            encode(solid(mode="CMYK"), "JPEG", icc_profile=SRGB_ICC, comment=b"x"),
            encode(noise((300, 300)), "WEBP"),
        ],
        ids=[
            "exif-jpeg",
            "big-jpeg",
            "gif",
            "animated-webp",
            "apng",
            "big-png",
            "cmyk-jpeg",
            "webp",
        ],
    )
    def test_processing_stored_bytes_again_changes_nothing(self, raw):
        first = image_service.process_image(raw)
        again = image_service.process_image(first.data)
        assert again == first


# ---------------------------------------------------------------------------
# Pixels, frames and error mapping (spec §2.5 steps 1–3)
# ---------------------------------------------------------------------------


class TestPixelsAndFrames:
    def test_header_pixel_check_runs_before_load(self, monkeypatch):
        def load(self):
            raise AssertionError("load() ran before the pixel check")

        monkeypatch.setattr(ImageFile.ImageFile, "load", load)
        with pytest.raises(UnprocessableError) as caught:
            image_service.process_image(png_header(8000, 6000))  # 48 MP
        assert caught.value.message == (
            "This image is 48 MP; the limit is 40 MP. Resize it and upload again."
        )

    def test_exactly_the_pixel_limit_passes_the_check(self):
        # 8000 × 5000 = 40 MP passes the check, then fails to decode (no pixel data).
        with pytest.raises(UnprocessableError) as caught:
            image_service.process_image(png_header(8000, 5000))
        assert caught.value.message == image_service.UNREADABLE_MESSAGE

    def test_decompression_bomb_error_is_mapped_with_megapixels(self):
        with pytest.raises(UnprocessableError) as caught:
            image_service.process_image(png_header(10000, 9000))  # 90 MP
        assert caught.value.status_code == 422
        assert "90 MP" in caught.value.message and "40 MP" in caught.value.message

    @pytest.mark.parametrize(
        ("pixels", "text"),
        [(48_000_000, "48 MP"), (40_000_001, "40.1 MP"), (None, "over 80 MP")],
    )
    def test_pixel_message(self, pixels, text):
        assert f"This image is {text}; the limit is 40 MP" in (
            image_service._pixel_message(pixels)
        )

    def test_gif_frames_are_never_counted(self, monkeypatch):
        def forbidden(self):
            raise AssertionError("GIF frames counted")

        real_seek = GifImagePlugin.GifImageFile.seek

        def seek(self, frame):
            if frame > 0:
                raise AssertionError(f"GIF seek to frame {frame}")
            return real_seek(self, frame)

        raw = animated("GIF", colors=[(n, 255 - n, 0) for n in range(0, 250, 5)])
        monkeypatch.setattr(
            GifImagePlugin.GifImageFile, "n_frames", property(forbidden)
        )
        monkeypatch.setattr(
            GifImagePlugin.GifImageFile, "is_animated", property(forbidden)
        )
        monkeypatch.setattr(GifImagePlugin.GifImageFile, "seek", seek)
        assert image_service.process_image(raw).content_type == "image/png"


class TestErrors:
    def test_input_over_the_limit_is_413_before_opening(self):
        with pytest.raises(PayloadTooLargeError) as caught:
            image_service.process_image(b"\0" * (image_service.INPUT_LIMIT_BYTES + 1))
        assert caught.value.status_code == 413
        assert "10 MiB" in caught.value.message

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"just some text",
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            encode(solid(), "TIFF"),
            encode(solid(), "BMP"),
        ],
        ids=["empty", "text", "svg", "tiff", "bmp"],
    )
    def test_unsupported_formats_are_415(self, raw):
        with pytest.raises(UnsupportedMediaTypeError) as caught:
            image_service.process_image(raw)
        assert caught.value.status_code == 415
        assert caught.value.message == image_service.UNSUPPORTED_MESSAGE

    def test_mpo_is_415(self):
        raw = encode(solid(), "MPO", save_all=True, append_images=[solid(color="blue")])
        with pytest.raises(UnsupportedMediaTypeError) as caught:
            image_service.process_image(raw)
        assert caught.value.message == image_service.MPO_MESSAGE

    @pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
    def test_truncated_images_are_422(self, fmt):
        raw = encode(noise((200, 200)), fmt)
        with pytest.raises(UnprocessableError) as caught:
            image_service.process_image(raw[: len(raw) // 2])
        assert caught.value.message == image_service.UNREADABLE_MESSAGE

    @pytest.mark.parametrize("error", [OSError, SyntaxError, ValueError, EOFError])
    def test_listed_pillow_errors_map_to_422(self, error):
        with pytest.raises(UnprocessableError):
            with image_service._reading_image():
                raise error("bad")

    def test_unidentified_is_mapped_before_oserror(self):
        with pytest.raises(UnsupportedMediaTypeError):
            with image_service._reading_image():
                raise PILImage.UnidentifiedImageError("?")

    def test_buzzer_errors_pass_through(self):
        error = PayloadTooLargeError("big")
        with pytest.raises(PayloadTooLargeError) as caught:
            with image_service._reading_image():
                raise error
        assert caught.value is error

    @pytest.mark.parametrize("error", [MemoryError, KeyError, RuntimeError])
    def test_anything_else_stays_a_server_error(self, error):
        with pytest.raises(error):
            with image_service._reading_image():
                raise error()

    def test_unexpected_failure_inside_the_pipeline_is_not_mapped(self, monkeypatch):
        def fail(img):
            raise MemoryError

        monkeypatch.setattr(image_service.ImageOps, "exif_transpose", fail)
        with pytest.raises(MemoryError):
            image_service.process_image(png_with_text(solid()))


# ---------------------------------------------------------------------------
# Reading uploads
# ---------------------------------------------------------------------------


class FakeUpload:
    """Like Starlette's UploadFile.read, optionally returning short reads."""

    def __init__(self, data: bytes, max_chunk: int | None = None):
        self._buffer = io.BytesIO(data)
        self._max_chunk = max_chunk
        self.bytes_read = 0

    async def read(self, size: int = -1) -> bytes:
        if self._max_chunk is not None and (size < 0 or size > self._max_chunk):
            size = self._max_chunk
        chunk = self._buffer.read(size)
        self.bytes_read += len(chunk)
        return chunk


class TestReadCapped:
    async def test_exactly_the_limit_is_accepted(self):
        assert await image_service.read_capped(FakeUpload(b"x" * 10), 10) == b"x" * 10

    async def test_over_the_limit_reads_at_most_limit_plus_one(self):
        upload = FakeUpload(b"x" * 500)
        with pytest.raises(PayloadTooLargeError):
            await image_service.read_capped(upload, 10)
        assert upload.bytes_read == 11

    async def test_short_reads_are_joined(self):
        upload = FakeUpload(b"abcdefgh", max_chunk=3)
        assert await image_service.read_capped(upload, 8) == b"abcdefgh"
        with pytest.raises(PayloadTooLargeError):
            await image_service.read_capped(FakeUpload(b"abcdefgh", max_chunk=3), 7)

    async def test_real_limit_message(self):
        upload = FakeUpload(b"\0" * (image_service.INPUT_LIMIT_BYTES + 1))
        with pytest.raises(PayloadTooLargeError) as caught:
            await image_service.read_capped(upload, image_service.INPUT_LIMIT_BYTES)
        assert caught.value.message == "The file is larger than the 10 MiB limit."

    async def test_process_upload_runs_the_real_pipeline(self):
        raw = encode(solid(), "PNG")
        result = await image_service.process_upload(FakeUpload(raw))
        assert result.data == raw


# ---------------------------------------------------------------------------
# Capacity: slots, waits and the import deadline (spec §2.5 step 0)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def limiter(monkeypatch) -> anyio.CapacityLimiter:
    # A fresh limiter per test: pytest-asyncio gives each test its own event loop (§4.15).
    fresh_limiter = anyio.CapacityLimiter(image_service.PROCESSING_SLOTS)
    monkeypatch.setattr(image_service, "PIPELINE_LIMITER", fresh_limiter)
    return fresh_limiter


class StubPipeline:
    """Stands in for process_image. The raw bytes say what a run does:

    - b"gate" or b"gate:<name>": block until `release(name)` (the default gate is "default")
    - b"fail": raise a 422
    - anything else: return at once

    `on_run[raw]`, if set, is called at the start of that run (to move a fake clock). Gates
    make timing irrelevant: a gated run is provably still running until the test releases it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Event] = {}
        self.on_run: dict[bytes, Callable[[], None]] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.started: list[bytes] = []
        self.finished: list[bytes] = []

    def _gate(self, name: str) -> threading.Event:
        with self._lock:
            return self._gates.setdefault(name, threading.Event())

    def release(self, name: str = "default"):
        self._gate(name).set()

    def __call__(self, raw: bytes) -> ProcessedImage:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.started.append(raw)
        try:
            if raw in self.on_run:
                self.on_run[raw]()
            if raw.startswith(b"gate"):
                name = raw.split(b":", 1)[1].decode() if b":" in raw else "default"
                assert self._gate(name).wait(timeout=10), f"gate {name} never released"
            elif raw == b"fail":
                raise UnprocessableError("bad image")
            return ProcessedImage(raw, b"", "image/png", 1, 1, len(raw), "")
        finally:
            with self._lock:
                self.in_flight -= 1
                self.finished.append(raw)


@pytest.fixture
def stub(monkeypatch) -> StubPipeline:
    pipeline = StubPipeline()
    monkeypatch.setattr(image_service, "process_image", pipeline)
    return pipeline


class FakeClock:
    """The clock `image_service` measures deadlines on (`_clock`), moved only by the test."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(image_service, "_clock", fake)
    return fake


async def eventually(condition, timeout: float = 10.0) -> None:
    """Wait for a condition another task or thread brings about. The timeout only bounds a
    failing test; a passing one never depends on how long this takes."""
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(0.01)


def entries(*raws: bytes) -> list[BundleEntry]:
    return [
        BundleEntry(key=f"img-{n}", filename="f.png", description="d", raw=raw)
        for n, raw in enumerate(raws, start=1)
    ]


async def upload(raw: bytes) -> ProcessedImage:
    return await image_service.process_upload(FakeUpload(raw))


class TestCapacity:
    async def test_at_most_two_run_and_the_rest_wait(self, stub, limiter):
        async with anyio.create_task_group() as tg:
            for _ in range(5):
                tg.start_soon(upload, b"gate")
            await eventually(lambda: len(stub.started) == 2)
            await eventually(lambda: limiter.statistics().tasks_waiting == 3)
            # A window for a third run to (wrongly) start; it can only make this test fail,
            # never pass by luck.
            await anyio.sleep(0.1)
            assert len(stub.started) == 2
            assert limiter.statistics().borrowed_tokens == 2
            stub.release()
        assert stub.max_in_flight == 2
        assert len(stub.finished) == 5
        assert limiter.borrowed_tokens == 0

    async def test_a_run_that_raises_releases_its_slot(self, stub, limiter):
        with pytest.raises(UnprocessableError):
            await upload(b"fail")
        assert limiter.borrowed_tokens == 0

    async def test_an_oversized_upload_releases_its_slot(
        self, stub, limiter, monkeypatch
    ):
        monkeypatch.setattr(image_service, "INPUT_LIMIT_BYTES", 3)
        with pytest.raises(PayloadTooLargeError):
            await upload(b"toolong")
        assert stub.started == []
        assert limiter.borrowed_tokens == 0

    async def test_a_cancelled_waiter_never_runs(self, stub, limiter):
        waiter_scope = anyio.CancelScope()

        async def waiter():
            with waiter_scope:
                await upload(b"third")

        async with anyio.create_task_group() as tg:
            tg.start_soon(upload, b"gate")
            tg.start_soon(upload, b"gate")
            await eventually(lambda: len(stub.started) == 2)
            tg.start_soon(waiter)
            await eventually(lambda: limiter.statistics().tasks_waiting == 1)
            waiter_scope.cancel()
            await eventually(lambda: limiter.statistics().tasks_waiting == 0)
            stub.release()
        assert b"third" not in stub.started
        assert limiter.borrowed_tokens == 0

    async def test_a_cancelled_run_finishes_before_its_slot_is_released(
        self, stub, limiter
    ):
        run_scope = anyio.CancelScope()
        done = anyio.Event()

        async def run():
            with run_scope:
                await upload(b"gate:run")
            done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await eventually(lambda: stub.started == [b"gate:run"])
            run_scope.cancel()
            # The run is gated, so it is certainly still going: cancelling the request must
            # neither interrupt it nor release its slot early.
            await anyio.sleep(0.1)
            assert not done.is_set()
            assert stub.finished == []
            assert limiter.borrowed_tokens == 1
            stub.release("run")
        assert stub.finished == [b"gate:run"]
        assert limiter.borrowed_tokens == 0

    async def test_waiting_past_the_slot_wait_limit_is_503(
        self, stub, limiter, monkeypatch
    ):
        # Both slots are held by gated runs, so the waiter's timeout is certain, not a race.
        monkeypatch.setattr(image_service, "SLOT_WAIT_SECONDS", 0.2)
        async with anyio.create_task_group() as tg:
            tg.start_soon(upload, b"gate")
            tg.start_soon(upload, b"gate")
            await eventually(lambda: len(stub.started) == 2)
            with pytest.raises(ServiceUnavailableError) as caught:
                await upload(b"third")
            assert caught.value.message == image_service.BUSY_MESSAGE
            assert caught.value.status_code == 503
            stub.release()
        assert b"third" not in stub.started


class TestImportDeadline:
    async def test_all_images_processed_in_order_within_the_deadline(
        self, stub, clock
    ):
        results = await image_service.process_bundle_images(
            entries(b"a", b"b", b"c"), clock.now + 10
        )
        assert [result.data for result in results] == [b"a", b"b", b"c"]

    async def test_deadline_expiring_while_waiting_is_503(self, stub, limiter, clock):
        # The slot wait limit (20 s) is far away and both slots are held by gated runs: only
        # the remaining 0.2 s before the deadline can end this wait.
        async with anyio.create_task_group() as tg:
            tg.start_soon(upload, b"gate")
            tg.start_soon(upload, b"gate")
            await eventually(lambda: len(stub.started) == 2)
            with pytest.raises(ServiceUnavailableError):
                await image_service.process_bundle_images(
                    entries(b"bundle-1"), clock.now + 0.2
                )
            stub.release()
        assert b"bundle-1" not in stub.started

    async def test_an_overdue_deadline_refuses_before_waiting(self, stub, clock):
        with pytest.raises(ServiceUnavailableError):
            await image_service.process_bundle_images(entries(b"a"), clock.now - 1)
        assert stub.started == []

    @pytest.mark.parametrize(
        "raws",
        [(b"fast-1", b"gate:slow", b"fast-3"), (b"fast-1", b"gate:slow")],
        ids=["middle-image", "final-image"],
    )
    async def test_deadline_expiring_during_a_decode(self, stub, limiter, clock, raws):
        deadline_at = clock.now + 10
        outcome: dict[str, Exception] = {}

        async def run_import():
            try:
                await image_service.process_bundle_images(entries(*raws), deadline_at)
            except ServiceUnavailableError as exc:
                outcome["error"] = exc

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_import)
            await eventually(lambda: b"gate:slow" in stub.started)
            # The deadline passes while that decode is still running (it's gated).
            clock.now = deadline_at + 1
            await anyio.sleep(0.1)
            assert b"gate:slow" not in stub.finished  # not interrupted…
            assert limiter.borrowed_tokens == 1  # …and still holding its slot
            stub.release("slow")

        # It completed, and only then was its slot released.
        assert b"gate:slow" in stub.finished
        assert limiter.borrowed_tokens == 0
        # The check after that image caught the deadline (for the final image too), and
        # nothing later ran.
        assert outcome["error"].message == image_service.IMPORT_DEADLINE_MESSAGE
        assert stub.started == [b"fast-1", b"gate:slow"]

    async def test_pipeline_errors_name_the_image(self, clock):
        raw = encode(solid(), "PNG")
        with pytest.raises(BuzzerError) as caught:
            await image_service.process_bundle_images(
                entries(raw, b"not an image"), clock.now + 10
            )
        assert caught.value.status_code == 415
        assert (
            caught.value.message == f"Image img-2: {image_service.UNSUPPORTED_MESSAGE}"
        )


class TestNoWritesAfterAnOverdueImport:
    """Through the real import_game, with the pipeline stubbed: an import whose processing
    phase passes the deadline creates nothing — not the game, not an image, not a question."""

    @staticmethod
    def bundle(*raws: bytes) -> bytes:
        images = [
            {
                "key": f"k{n}",
                "filename": f"f{n}.png",
                "description": f"Image {n}",
                "data": base64.b64encode(raw).decode(),
            }
            for n, raw in enumerate(raws, start=1)
        ]
        question = {
            "type": "multiple_choice",
            "grading_type": "ACCURACY",
            "prompt": "Which?",
            "config": {
                "prompt_image_id": "k1",
                "options": ["", "B"],
                "option_images": ["k2", None],
            },
            "answer_data": {"answer_points": [1, 0]},
            "points_value": 1,
        }
        return json.dumps(
            {
                "format": "buzzer/game",
                "version": 2,
                "game": {"title": "Overdue"},
                "images": images,
                "questions": [question],
            }
        ).encode()

    @pytest.mark.parametrize("overdue", [b"img2", b"img3"], ids=["middle", "final"])
    async def test_nothing_is_written(self, stub, clock, monkeypatch, overdue):
        from app.services import game_management_service

        created = []

        async def create_game_spy(*args, **kwargs):
            created.append((args, kwargs))
            raise AssertionError("create_game ran after an overdue import")

        monkeypatch.setattr(game_management_service, "create_game", create_game_spy)
        monkeypatch.setattr(image_service, "IMPORT_DEADLINE_SECONDS", 100.0)
        # This image's decode takes "too long": the fake clock jumps past the deadline.
        stub.on_run[overdue] = lambda: setattr(clock, "now", clock.now + 1000)
        db = AsyncMock()

        with pytest.raises(ServiceUnavailableError) as caught:
            await game_management_service.import_game(
                db,
                self.bundle(b"img1", b"img2", b"img3"),
                course_id=1,
                creator=MagicMock(role="ADMIN"),
            )
        assert caught.value.status_code == 503
        assert stub.started == [b"img1", b"img2", b"img3"][: stub.started.index(overdue) + 1]
        assert created == []
        assert db.method_calls == []  # no add, flush, execute or commit of any kind


# ---------------------------------------------------------------------------
# Links and tokens (spec §2.9)
# ---------------------------------------------------------------------------


def fake_image(image_id=7, version=2, width=640, height=480, description="A tower"):
    return SimpleNamespace(
        id=image_id, version=version, width=width, height=height, description=description
    )


class TestLinks:
    def test_expiry_is_the_start_of_the_hour_plus_three_hours(self):
        at = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        for minute, second in [(0, 0), (30, 15), (59, 59)]:
            now = at.replace(minute=minute, second=second)
            assert image_service.link_expiry(now) == at + timedelta(hours=3)
        assert image_service.link_expiry(at + timedelta(hours=1)) == at + timedelta(hours=4)

    def test_same_hour_same_url_new_hour_new_url(self):
        image = fake_image()
        noon = datetime(2026, 9, 28, 12, 1, tzinfo=timezone.utc)
        url, expires_at = image_service.link(image, "full", noon)
        assert url == image_service.link(image, "full", noon + timedelta(minutes=58))[0]
        assert url != image_service.link(image, "full", noon + timedelta(hours=1))[0]
        assert url.startswith("/api/images/7/v2/full?t=")
        assert expires_at == "2026-09-28T15:00:00Z"

    def test_variant_and_version_change_the_url(self):
        noon = datetime(2026, 9, 28, 12, 1, tzinfo=timezone.utc)
        full, _ = image_service.link(fake_image(), "full", noon)
        small, _ = image_service.link(fake_image(), "small", noon)
        replaced, _ = image_service.link(fake_image(version=3), "full", noon)
        assert len({full, small, replaced}) == 3

    def test_token_claims(self):
        exp = datetime.now(timezone.utc) + timedelta(hours=2)
        token = auth_service.create_image_token(7, 2, "small", exp)
        claims = auth_service.decode_token(token)
        assert claims == {
            "token_type": "image",
            "img": 7,
            "v": 2,
            "var": "small",
            "exp": int(exp.timestamp()),
        }
        assert "sub" not in claims and "iat" not in claims
        assert token == auth_service.create_image_token(7, 2, "small", exp)

    def test_verify_accepts_only_the_exact_path(self):
        exp = datetime.now(timezone.utc) + timedelta(hours=2)
        token = auth_service.create_image_token(7, 2, "full", exp)
        assert image_service.verify_link(7, 2, "full", token) == int(exp.timestamp())
        for image_id, version, variant in [(8, 2, "full"), (7, 1, "full"), (7, 2, "small")]:
            with pytest.raises(ForbiddenError):
                image_service.verify_link(image_id, version, variant, token)

    @pytest.mark.parametrize("case", ["tampered", "expired", "access", "garbage", "empty"])
    def test_verify_refuses_bad_tokens(self, case):
        now = datetime.now(timezone.utc)
        token = {
            "tampered": auth_service.create_image_token(7, 2, "full", now + timedelta(hours=2))[:-4] + "AAAA",
            "expired": auth_service.create_image_token(7, 2, "full", now - timedelta(minutes=1)),
            "access": auth_service.create_access_token("user-1", "ADMIN"),
            "garbage": "not-a-token",
            "empty": "",
        }[case]
        with pytest.raises(ForbiddenError) as caught:
            image_service.verify_link(7, 2, "full", token)
        assert caught.value.status_code == 403

    def test_utc_z_formats_naive_database_times_and_aware_times(self):
        assert image_service.utc_z(datetime(2026, 9, 28, 18, 0, 5, 999)) == "2026-09-28T18:00:05Z"
        eastern = timezone(timedelta(hours=-5))
        assert image_service.utc_z(datetime(2026, 9, 28, 13, 0, tzinfo=eastern)) == "2026-09-28T18:00:00Z"


class TestPlayerImages:
    def test_option_images_only_small_links_missing_ids_skipped(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        images = {5: fake_image(image_id=5), 6: fake_image(image_id=6, description="Six")}
        config = {"prompt_image_id": 6, "options": ["a", "b", "c"], "option_images": [5, 999, 5]}
        result = image_service.player_images(config, images, now)
        assert list(result) == ["5"]
        entry = result["5"]
        assert entry["url"].startswith("/api/images/5/v2/small?t=")
        assert (entry["width"], entry["height"], entry["description"]) == (640, 480, "A tower")

    def test_no_option_images_is_an_empty_map(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        assert image_service.player_images({"prompt_image_id": 5}, {5: fake_image(5)}, now) == {}


# ---------------------------------------------------------------------------
# Validation helpers, uses and budgets
# ---------------------------------------------------------------------------


class TestValidationHelpers:
    @pytest.mark.parametrize("text", ["", "   ", "x" * 301])
    def test_bad_descriptions_are_422(self, text):
        with pytest.raises(UnprocessableError):
            image_service.clean_description(text)

    def test_descriptions_are_trimmed(self):
        assert image_service.clean_description("  A tower  ") == "A tower"
        assert image_service.clean_description("x" * 300) == "x" * 300

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("C:\\Users\\me\\tower.jpg", "tower.jpg"),
            ("../../etc/passwd", "passwd"),
            ("bad\x00\x07name.png", "badname.png"),
            ("", "image"),
            (None, "image"),
            ("dir/", "image"),
            ("a" * 300, "a" * 255),
        ],
    )
    def test_filenames_are_display_only_basenames(self, name, expected):
        assert image_service.clean_filename(name) == expected


class TestUsesAndBudgets:
    def test_used_by_follows_question_order_and_ignores_unresolved(self):
        questions = [
            (11, {"prompt_image_id": 5}),
            (12, {"options": ["a", "b"], "option_images": [None, 5]}),
            (13, {"prompt_image_id": 999, "image_id": 5}),
        ]
        usage = image_service.used_by_map(questions, {5})
        assert [(u.question_id, u.number, u.role, u.option_letter) for u in usage[5]] == [
            (11, 1, "prompt", None),
            (12, 2, "option", "B"),
            (13, 3, "canvas", None),
        ]
        assert 999 not in usage
        assert image_service.describe_uses(usage[5]) == "Q1 (prompt), Q2 (option B), Q3 (canvas)"

    def test_budget_messages_name_the_limit_and_the_value(self):
        with pytest.raises(ConflictError) as caught:
            image_service.check_budgets(51, 0)
        assert "51 images" in caught.value.message and "limit is 50" in caught.value.message
        over = image_service.LIBRARY_BUDGET_BYTES + 1
        with pytest.raises(ConflictError) as caught:
            image_service.check_budgets(3, over)
        assert f"{over:,} bytes" in caught.value.message and "24 MiB" in caught.value.message
        image_service.check_budgets(50, image_service.LIBRARY_BUDGET_BYTES)


def bundle_entry(**overrides):
    entry = {
        "key": "img-1",
        "filename": "dir/tower.png",
        "description": "  A tower ",
        "data": base64.b64encode(b"\x89PNG-bytes").decode(),
    }
    entry.update(overrides)
    return entry


class TestBundleEntries:
    def test_valid_entries_are_decoded_and_cleaned(self):
        [entry] = image_service.check_bundle_entries([bundle_entry()])
        assert entry == BundleEntry("img-1", "tower.png", "A tower", b"\x89PNG-bytes")

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ({**bundle_entry(), "content_type": "image/png"}, "Extra inputs are not permitted"),
            ({k: v for k, v in bundle_entry().items() if k != "data"}, "data: Field required"),
            (bundle_entry(key="bad key!"), "key: String should match pattern"),
            (bundle_entry(key="x" * 65), "key: String should match pattern"),
            (bundle_entry(filename=5), "filename: Input should be a valid string"),
            (bundle_entry(data="not base64!"), "data isn't valid base64"),
            (bundle_entry(data="QUJD\nREVG"), "data isn't valid base64"),
            (bundle_entry(description=""), "Describe the image"),
            ("just a string", "Image entry 1:"),
        ],
    )
    def test_malformed_entries_are_422(self, raw, message):
        with pytest.raises(UnprocessableError) as caught:
            image_service.check_bundle_entries([raw])
        assert message in caught.value.message

    def test_duplicate_keys_are_422(self):
        with pytest.raises(UnprocessableError) as caught:
            image_service.check_bundle_entries([bundle_entry(), bundle_entry()])
        assert "img-1" in caught.value.message and "more than one" in caught.value.message

    def test_data_over_the_input_limit_is_413(self, monkeypatch):
        monkeypatch.setattr(image_service, "INPUT_LIMIT_BYTES", 8)
        with pytest.raises(PayloadTooLargeError):
            image_service.check_bundle_entries(
                [bundle_entry(data=base64.b64encode(b"123456789").decode())]
            )


# ---------------------------------------------------------------------------
# The game lock and fresh reads (spec §2.6, §4.4)
# ---------------------------------------------------------------------------


def mysql_sql(stmt) -> str:
    return str(stmt.compile(dialect=mysql.dialect()))


class TestFresh:
    def test_shared_by_default(self):
        stmt = fresh(select(Game).where(Game.id == 1))
        assert mysql_sql(stmt).endswith("LOCK IN SHARE MODE")
        assert stmt.get_execution_options()["populate_existing"] is True

    def test_exclusive(self):
        stmt = fresh(select(Game).where(Game.id == 1), exclusive=True)
        assert mysql_sql(stmt).endswith("FOR UPDATE")
        assert stmt.get_execution_options()["populate_existing"] is True


def session_returning(game=None, error=None) -> AsyncMock:
    db = AsyncMock()
    if error is not None:
        db.execute.side_effect = error
    else:
        result = MagicMock()
        result.scalar_one_or_none.return_value = game
        db.execute.return_value = result
    return db


def mysql_error(code: int) -> OperationalError:
    return OperationalError("SELECT …", {}, Exception(code, "MySQL says no"))


class TestLockGame:
    async def test_locks_the_game_row_exclusively_and_fresh(self):
        game = Game(id=7, title="t")
        db = session_returning(game)
        assert await lock_game(db, 7) is game
        [stmt] = db.execute.await_args.args
        sql = mysql_sql(stmt)
        assert sql.startswith("SELECT") and "FROM games" in sql
        assert sql.endswith("FOR UPDATE")
        assert stmt.get_execution_options()["populate_existing"] is True

    async def test_missing_game_is_404(self):
        with pytest.raises(NotFoundError):
            await lock_game(session_returning(None), 7)

    @pytest.mark.parametrize("code", [1205, 1213])
    async def test_lock_timeout_and_deadlock_are_409(self, code):
        with pytest.raises(ConflictError) as caught:
            await lock_game(session_returning(error=mysql_error(code)), 7)
        assert caught.value.message == LOCK_CONFLICT_MESSAGE

    async def test_other_database_errors_are_not_mapped(self):
        with pytest.raises(OperationalError):
            await lock_game(session_returning(error=mysql_error(2013)), 7)


# ---------------------------------------------------------------------------
# The model, as MySQL would create it (the migration is checked against a disposable
# database in §4.12; this only pins the model's shape)
# ---------------------------------------------------------------------------


class TestImageModel:
    def test_table_shape(self):
        ddl = str(CreateTable(Image.__table__).compile(dialect=mysql.dialect()))
        assert "data MEDIUMBLOB NOT NULL" in ddl
        assert "small_data MEDIUMBLOB NOT NULL" in ddl
        assert "sha256 CHAR(64) NOT NULL" in ddl
        assert "description VARCHAR(300) NOT NULL" in ddl
        assert "version INTEGER NOT NULL DEFAULT '1'" in ddl
        assert (
            "CONSTRAINT fk_images_game_id FOREIGN KEY(game_id) "
            "REFERENCES games (id) ON DELETE CASCADE"
        ) in ddl
        assert {index.name for index in Image.__table__.indexes} == {
            "ix_images_game_id"
        }

    def test_blobs_are_deferred(self):
        for column in ("data", "small_data"):
            assert Image.__mapper__.column_attrs[column].deferred

    def test_game_images_leave_deletes_to_the_database(self):
        relationship = Game.__mapper__.relationships["images"]
        assert relationship.passive_deletes is True
        assert relationship.cascade.delete_orphan
