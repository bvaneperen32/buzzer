"""Images (T8): the processing pipeline, its capacity limits, game image libraries and links.

**The pipeline.** This is the only code that accepts image bytes. Every image entering the
system (upload, replace or import) goes through `process_image`, which implements the T8 spec
§2.5:

0. capacity: callers hold a processing slot (`processing_slot`)
1. size: over the input limit → 413
2. format: PNG, JPEG, WebP or GIF, from the bytes → otherwise 415
3. pixels from the header (→ 422) before a full decode of the first frame (→ 422)
4. conformance: conforming bytes are stored unchanged
5. normalization of anything else, which always produces conforming bytes
6. the small copy, always WebP
7. every derived field from the stored bytes

Normalized output conforms, so processing is idempotent and full images survive any number of
export/import cycles byte for byte.

**Libraries.** Each game owns its images. Every write takes the game row lock (`lock_game`),
makes the spec §2.6 fresh reads, checks the would-be state (budgets, the export rule, the live
guard, references), and only then changes rows. Access checks are the caller's job.

**Links.** Browsers load images through signed, expiring URLs built only here and only where
access has already been checked (T8 spec §2.9).

Imports only image_refs, bundle_service, game_lock, auth_service, state_service, models,
schemas.image and common.exceptions (T8 spec §4.4). Never game_service or
game_management_service, which keeps T7's future `game_service → image_service` cycle-free.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import unicodedata
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import anyio
import anyio.to_thread
from jose import JWTError
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ..common.exceptions import (
    BuzzerError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnprocessableError,
    UnsupportedMediaTypeError,
)
from ..models.game import Game, Question
from ..models.image import Image
from ..models.session import GameSession
from ..models.user import User
from ..schemas.image import (
    BundleImage,
    ImageItem,
    LibraryMeters,
    LibraryResponse,
    LinkEntry,
    LiveSessionItem,
    SessionImages,
    UsedBy,
)
from . import auth_service, bundle_service
from . import state_service as state
from .bundle_service import GameMeta, ImageRecord, QuestionFields
from .game_lock import fresh, lock_game
from .image_refs import OPTION, image_refs, is_image_id

# ── Limits (T8 spec §2.4, the only definition of each) ───────────────────────

MIB = 1024 * 1024
INPUT_LIMIT_BYTES = 10 * MIB
PIXEL_LIMIT = 40_000_000
FULL_EDGE = 1600
FULL_MAX_BYTES = 4 * MIB
SMALL_EDGE = 400
SMALL_MAX_BYTES = 1 * MIB
IMAGE_COUNT_LIMIT = 50
LIBRARY_BUDGET_BYTES = 24 * MIB
PROCESSING_SLOTS = 2
SLOT_WAIT_SECONDS = 20.0
IMPORT_DEADLINE_SECONDS = 240.0

JPEG_QUALITY = 85
WEBP_QUALITY = 85
SMALL_FALLBACK_QUALITY = 50

# A backstop only: Pillow raises DecompressionBombError at twice this, from Image.open. The
# explicit header check in `_open_input` is the enforcement. No warning filters are used for
# the band in between: warnings.catch_warnings() isn't thread-safe, and this runs in threads.
PILImage.MAX_IMAGE_PIXELS = PIXEL_LIMIT

ACCEPTED_FORMATS = ("PNG", "JPEG", "WEBP", "GIF")
CONFORMING_FORMATS = ("PNG", "JPEG", "WEBP")
CONTENT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
# JPEG stays JPEG, WebP stays WebP, PNG and GIF become PNG (T8 spec §2.5 step 5).
_NORMALIZED_FORMAT = {"JPEG": "JPEG", "WEBP": "WEBP", "PNG": "PNG", "GIF": "PNG"}
_QUALITY = {"JPEG": JPEG_QUALITY, "WEBP": WEBP_QUALITY, "PNG": None}
_METADATA_INFO_KEYS = ("exif", "xmp", "XML:com.adobe.xmp", "photoshop", "comment")

BUSY_MESSAGE = "Image processing is busy — try again in a moment."
IMPORT_DEADLINE_MESSAGE = (
    "Processing this bundle's images took too long, so nothing was imported. "
    "Try again when the server is less busy."
)
UNSUPPORTED_MESSAGE = (
    "This file type isn't supported. Upload a PNG, JPEG, WebP or GIF image."
)
MPO_MESSAGE = (
    "This JPEG holds several pictures (MPO), which isn't supported. "
    "Save it as a regular JPEG and upload again."
)
UNREADABLE_MESSAGE = "The image couldn't be read."
FULL_TOO_LARGE_MESSAGE = (
    f"The image is still larger than {FULL_MAX_BYTES // MIB} MiB after resizing "
    "and compression. Upload a smaller or simpler image."
)
SMALL_TOO_LARGE_MESSAGE = (
    f"The image's preview can't be made smaller than {SMALL_MAX_BYTES // MIB} MiB. "
    "Upload a smaller or simpler image."
)


@dataclass(frozen=True)
class ProcessedImage:
    """What the pipeline stores. Every field is derived from the stored bytes."""

    data: bytes
    small_data: bytes
    content_type: str
    width: int
    height: int
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class BundleEntry:
    """One bundle image that passed the checks that need no decoding (T8 spec §2.15 step 4)."""

    key: str
    filename: str
    description: str
    raw: bytes


# ── Capacity ─────────────────────────────────────────────────────────────────

# Created at import: anyio 4.15 returns an adapter that binds to the event loop on first use.
# Always read through the module attribute (never bound as a default argument), so tests can
# replace it with a fresh limiter per event loop.
PIPELINE_LIMITER = anyio.CapacityLimiter(PROCESSING_SLOTS)


def _clock() -> float:
    """The clock every deadline here is measured on: the event loop's monotonic time.

    One seam, so tests can move time past a deadline while a gated decode runs instead of
    racing real sleeps.
    """
    return anyio.current_time()


def import_deadline() -> float:
    """The deadline for an import's image-processing phase, starting now."""
    return _clock() + IMPORT_DEADLINE_SECONDS


@asynccontextmanager
async def processing_slot(deadline_at: float | None = None) -> AsyncIterator[None]:
    """Hold one processing slot, waiting at most the slot wait limit or until `deadline_at`.

    Waiting too long → 503. The slot is released on exit, including after an exception or
    cancellation. A worker-thread run inside always finishes before that, because
    `to_thread.run_sync` doesn't return until its thread does.
    """
    limiter = PIPELINE_LIMITER
    wait = SLOT_WAIT_SECONDS
    if deadline_at is not None:
        wait = min(wait, deadline_at - _clock())
    try:
        # Bounds only the wait for a token, never the work done while holding one.
        with anyio.fail_after(max(wait, 0.0)):
            await limiter.acquire()
    except TimeoutError:
        raise ServiceUnavailableError(BUSY_MESSAGE) from None
    try:
        yield
    finally:
        limiter.release()


class Readable(Protocol):
    """An uploaded file, such as Starlette's `UploadFile`."""

    async def read(self, size: int = -1) -> bytes: ...


def _over_limit_message(limit: int) -> str:
    return f"The file is larger than the {limit // MIB} MiB limit."


async def read_capped(upload: Readable, limit: int) -> bytes:
    """Read at most `limit` + 1 bytes; anything longer → 413.

    This bounds only the handler's own copy. FastAPI has already received the whole body by
    the time a handler runs, so nginx's per-route limits are the real gate on body size.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = await upload.read(limit + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise PayloadTooLargeError(_over_limit_message(limit))
    return b"".join(chunks)


async def process_upload(upload: Readable) -> ProcessedImage:
    """Slot, capped read, pipeline. Used by upload and replace.

    The slot is taken before reading, so a waiting request holds only Starlette's spooled temp
    file, not the bytes. The pipeline runs in a worker thread so the event loop, which also
    serves the game's sockets, stays responsive. `run_sync` keeps its default
    `abandon_on_cancel=False`: a run that has started finishes even if the request is
    cancelled, and its slot is released only afterwards. No deadline applies to an upload.
    """
    async with processing_slot():
        raw = await read_capped(upload, INPUT_LIMIT_BYTES)
        return await anyio.to_thread.run_sync(process_image, raw)


async def process_bundle_images(
    entries: Sequence[BundleEntry], deadline_at: float
) -> list[ProcessedImage]:
    """An import's image-processing phase (T8 spec §2.15 step 6): one slot per image, in turn.

    After each image, including the last, the time is checked against `deadline_at` (on
    `_clock()`, see `import_deadline`). Once past it → 503, and every processed result is dropped:
    the caller must not write anything. A decode already running when the deadline passes
    can't be interrupted, so the phase can overrun by up to one image's processing time.
    """
    processed: list[ProcessedImage] = []
    for entry in entries:
        async with processing_slot(deadline_at):
            try:
                result = await anyio.to_thread.run_sync(process_image, entry.raw)
            except BuzzerError as exc:
                raise BuzzerError(
                    exc.code, f"Image {entry.key}: {exc.message}", exc.status_code
                ) from exc
        processed.append(result)
        if _clock() > deadline_at:
            raise ServiceUnavailableError(IMPORT_DEADLINE_MESSAGE)
    return processed


# ── The pipeline ─────────────────────────────────────────────────────────────


def _megapixels(pixels: int) -> str:
    # Rounded up to tenths, so an image just over the limit never reads as "40 MP".
    tenths = -(-pixels // 100_000)
    return str(tenths // 10) if tenths % 10 == 0 else f"{tenths // 10}.{tenths % 10}"


def _pixel_message(pixels: int | None) -> str:
    limit = _megapixels(PIXEL_LIMIT)
    size = (
        f"{_megapixels(pixels)} MP"
        if pixels is not None
        else f"over {_megapixels(2 * PIXEL_LIMIT)} MP"
    )
    return f"This image is {size}; the limit is {limit} MP. Resize it and upload again."


def _bomb_pixels(exc: PILImage.DecompressionBombError) -> int | None:
    # Pillow's message: "Image size (90000000 pixels) exceeds limit of …".
    match = re.search(r"\((\d+) pixels\)", str(exc))
    return int(match.group(1)) if match else None


@contextmanager
def _reading_image() -> Iterator[None]:
    """Map Pillow's errors on the input bytes to client statuses (T8 spec §2.5 step 3).

    Wraps only Pillow's operations: opening, reading the header, decoding, transforming and
    encoding. Anything not listed (an application bug, MemoryError, an unlisted Pillow error)
    stays a logged 500. `UnidentifiedImageError` subclasses `OSError`, so it comes first.
    """
    try:
        yield
    except BuzzerError:
        raise
    except UnidentifiedImageError as exc:
        raise UnsupportedMediaTypeError(UNSUPPORTED_MESSAGE) from exc
    except PILImage.DecompressionBombError as exc:
        raise UnprocessableError(_pixel_message(_bomb_pixels(exc))) from exc
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        raise UnprocessableError(UNREADABLE_MESSAGE) from exc


def _open_input(raw: bytes) -> PILImage.Image:
    """Identify the format, check the pixel count from the header, then decode frame 0."""
    with _reading_image():
        # Only the accepted formats' parsers ever run on uploaded bytes.
        img = PILImage.open(io.BytesIO(raw), formats=ACCEPTED_FORMATS)
    if img.format not in ACCEPTED_FORMATS:
        # The JPEG parser opens multi-picture files as MPO.
        raise UnsupportedMediaTypeError(
            MPO_MESSAGE if img.format == "MPO" else UNSUPPORTED_MESSAGE
        )
    width, height = img.size
    if width * height > PIXEL_LIMIT:
        raise UnprocessableError(_pixel_message(width * height))
    with _reading_image():
        img.load()
    return img


def _has_metadata(img: PILImage.Image) -> bool:
    # Cheap key checks first: getexif() parses, and PNG text is complete only after load().
    if any(key in img.info for key in _METADATA_INFO_KEYS):
        return True
    if img.format == "JPEG" and any(
        marker in ("APP1", "APP13") for marker, _ in img.applist
    ):
        return True
    if img.format == "PNG" and img.text:
        return True
    return len(img.getexif()) > 0


def _nonconformance(img: PILImage.Image, data: bytes) -> str | None:
    """The first T8 spec §2.5 step 4 condition `data` fails, or None if it conforms.

    `img` must be `data` opened and loaded. The order matters: animation is checked before
    anything that could read further frames (PNG's `text` seeks to an APNG's last frame).
    """
    if img.format not in CONFORMING_FORMATS:
        return "format"
    # From the header for PNG and WebP. GIF never gets here, so its frames are never counted.
    if getattr(img, "is_animated", False):
        return "animated"
    width, height = img.size
    if width < 1 or height < 1 or max(width, height) > FULL_EDGE:
        return "dimensions"
    if _has_metadata(img):
        return "metadata"
    if len(data) > FULL_MAX_BYTES:
        return "size"
    return None


def conforms(img: PILImage.Image, data: bytes) -> bool:
    """Whether `data` (opened and loaded as `img`) can be stored unchanged."""
    return _nonconformance(img, data) is None


def _colour_space(mode: str) -> str:
    if mode == "CMYK":
        return "cmyk"
    if mode in ("1", "L", "LA", "La", "I", "F") or mode.startswith("I;16"):
        return "grey"
    return "rgb"


def _strip_info(img: PILImage.Image) -> None:
    """Drop every `info` entry except `transparency`, in place, on an image the pipeline owns.

    Pillow's encoders fall back to `info` (JPEG writes `info["comment"]` back), and `info`
    survives copies, transposes and resizes. The ICC profile is passed to `_encode` instead.
    """
    kept = img.info.get("transparency")
    img.info = {} if kept is None else {"transparency": kept}


def _to_rgb(img: PILImage.Image) -> PILImage.Image:
    """RGB or RGBA, as WebP stores. 16-bit greyscale is scaled to 8 bits, not clipped."""
    if img.mode in ("RGB", "RGBA"):
        return img
    if img.mode == "I" or img.mode.startswith("I;16"):
        img = img.convert("I").point(lambda value: value * (1 / 256)).convert("L")
    return img.convert("RGBA" if img.has_transparency_data else "RGB")


def _resampleable(img: PILImage.Image) -> PILImage.Image:
    """A mode Pillow resamples with the filter asked for.

    Pillow silently uses nearest-neighbour for palette and 1-bit images, which drops thin
    lines and aliases edges when downscaling.
    """
    if img.mode == "1":
        return img.convert("L")
    if img.mode in ("P", "PA"):
        return img.convert("RGBA" if img.has_transparency_data else "RGB")
    return img


def _encode(
    img: PILImage.Image,
    fmt: str,
    *,
    icc: bytes | None,
    source_mode: str,
    quality: int | None,
) -> bytes:
    """Encode without metadata. The ICC profile is kept only while the pixels stay in its
    colour space (a CMYK or greyscale profile is wrong on RGB pixels)."""
    if fmt == "WEBP":
        img = _to_rgb(img)
    same_space = _colour_space(source_mode) == _colour_space(img.mode)
    options = {} if quality is None else {"quality": quality}
    buffer = io.BytesIO()
    img.save(buffer, fmt, icc_profile=icc if same_space else None, **options)
    return buffer.getvalue()


def _normalize(img: PILImage.Image) -> bytes:
    """Orientation applied, first frame only, metadata dropped except ICC, scaled down to the
    full-image edge, re-encoded (T8 spec §2.5 step 5). The result may still be too large."""
    icc = img.info.get("icc_profile")
    # A new image: the current (first) frame, oriented.
    frame = ImageOps.exif_transpose(img)
    _strip_info(frame)
    if max(frame.size) > FULL_EDGE:
        frame = _resampleable(frame)
        frame.thumbnail((FULL_EDGE, FULL_EDGE), PILImage.Resampling.LANCZOS)
    fmt = _NORMALIZED_FORMAT[img.format]
    data = _encode(frame, fmt, icc=icc, source_mode=img.mode, quality=_QUALITY[fmt])
    if len(data) > FULL_MAX_BYTES:
        data = _encode(
            frame, "WEBP", icc=icc, source_mode=img.mode, quality=WEBP_QUALITY
        )
    return data


def _decode_stored(data: bytes) -> PILImage.Image:
    """Open bytes the pipeline itself produced. Failing here is a bug, so errors aren't mapped."""
    img = PILImage.open(io.BytesIO(data), formats=CONFORMING_FORMATS)
    img.load()
    return img


def _small_copy(full: PILImage.Image) -> bytes:
    """The small copy of the stored full image: always WebP, transparency kept."""
    small = _to_rgb(full)
    if small is full:
        small = full.copy()  # thumbnail() resizes in place
    _strip_info(small)
    small.thumbnail((SMALL_EDGE, SMALL_EDGE), PILImage.Resampling.LANCZOS)
    icc = full.info.get("icc_profile")
    for quality in (WEBP_QUALITY, SMALL_FALLBACK_QUALITY):
        data = _encode(small, "WEBP", icc=icc, source_mode=full.mode, quality=quality)
        if len(data) <= SMALL_MAX_BYTES:
            return data
    raise PayloadTooLargeError(SMALL_TOO_LARGE_MESSAGE)


def process_image(raw: bytes) -> ProcessedImage:
    """The T8 spec §2.5 pipeline. Synchronous and CPU-bound: async callers run it in a worker
    thread (`process_upload`, `process_bundle_images`)."""
    if len(raw) > INPUT_LIMIT_BYTES:
        raise PayloadTooLargeError(_over_limit_message(INPUT_LIMIT_BYTES))
    img = _open_input(raw)
    with _reading_image():
        reason = _nonconformance(img, raw)
    if reason is None:
        full, data = img, raw
    else:
        with _reading_image():
            data = _normalize(img)
        full = _decode_stored(data)
        reason = _nonconformance(full, data)
        if reason == "size":
            raise PayloadTooLargeError(FULL_TOO_LARGE_MESSAGE)
        if reason is not None:
            # Normalization guarantees every other condition (T8 spec §2.5 step 5).
            raise RuntimeError(f"Normalized image doesn't conform: {reason}")
    with _reading_image():
        small_data = _small_copy(full)
    width, height = full.size
    return ProcessedImage(
        data=data,
        small_data=small_data,
        content_type=CONTENT_TYPES[full.format],
        width=width,
        height=height,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


# ── Validation helpers ───────────────────────────────────────────────────────

DESCRIPTION_MAX = 300
FILENAME_MAX = 255


def clean_description(text: str) -> str:
    """Trimmed; 1–300 characters, else 422. The image's accessible text (alt text)."""
    cleaned = text.strip() if isinstance(text, str) else ""
    if not 1 <= len(cleaned) <= DESCRIPTION_MAX:
        raise UnprocessableError(
            f"Describe the image in 1–{DESCRIPTION_MAX} characters: what it shows, "
            "not which answer is correct."
        )
    return cleaned


def clean_filename(name: str | None) -> str:
    """A display-only basename: no path, no control characters, at most 255 characters."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) != "Cc").strip()
    return base[:FILENAME_MAX] or "image"


def _mib(size: int) -> str:
    return f"{size / MIB:.2f}"


# ── Timestamps and links (T8 spec §2.9, §2.17) ──────────────────────────────

VARIANTS = ("full", "small")
LINK_LIFETIME_HOURS = 3


def utc_z(value: datetime) -> str:
    """ISO 8601 UTC with whole seconds and a `Z`. Naive values are MySQL `NOW()` times, UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def link_expiry(now: datetime) -> datetime:
    """The start of the current UTC hour plus 3 hours: every link is valid for 2–3 hours, and
    links made within one hour are identical, so browsers reuse the cached bytes."""
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return hour + timedelta(hours=LINK_LIFETIME_HOURS)


def link(image: Image, variant: str, now: datetime) -> tuple[str, str]:
    """(root-relative URL, `expires_at`) for one variant of the image's current version."""
    expires = link_expiry(now)
    token = auth_service.create_image_token(image.id, image.version, variant, expires)
    return f"/api/images/{image.id}/v{image.version}/{variant}?t={token}", utc_z(
        expires
    )


def link_entry(image: Image, now: datetime) -> LinkEntry:
    url, expires_at = link(image, "full", now)
    small_url, _ = link(image, "small", now)
    return LinkEntry(
        url=url,
        small_url=small_url,
        width=image.width,
        height=image.height,
        description=image.description,
        expires_at=expires_at,
    )


LINK_REFUSED_MESSAGE = "This image link is invalid or has expired."


def verify_link(image_id: int, version: int, variant: str, token: str) -> int:
    """403 unless `token` is a valid, unexpired image token for exactly this path.

    Returns the token's expiry (a Unix timestamp). An access or temp token never passes: its
    `token_type` isn't "image".
    """
    try:
        claims = auth_service.decode_token(token)
    except JWTError:
        raise ForbiddenError(LINK_REFUSED_MESSAGE) from None
    if (
        claims.get("token_type") != "image"
        or claims.get("img") != image_id
        or claims.get("v") != version
        or claims.get("var") != variant
        or variant not in VARIANTS
    ):
        raise ForbiddenError(LINK_REFUSED_MESSAGE)
    return int(claims["exp"])


async def read_image_bytes(
    db: AsyncSession, image_id: int, version: int, variant: str
) -> tuple[bytes, str]:
    """(bytes, content type) of one variant, loading only that blob. A missing image or a
    superseded version → 404."""
    blob = Image.data if variant == "full" else Image.small_data
    row = (
        await db.execute(
            select(Image.version, Image.content_type, blob).where(Image.id == image_id)
        )
    ).one_or_none()
    if row is None or row[0] != version:
        raise NotFoundError("Image not found")
    return row[2], row[1] if variant == "full" else "image/webp"


# ── Live sessions (T8 spec §2.2, §2.13) ─────────────────────────────────────


async def live_sessions(
    db: AsyncSession, redis: Redis, game_id: int, *, locking: bool
) -> list[LiveSessionItem]:
    """The game's LOBBY/IN_PROGRESS sessions whose Redis room still exists.

    Without the room key a game can't continue, so a long-abandoned lobby doesn't block the
    library forever. The live guards pass `locking=True` (a fresh shared read, under the game
    lock); `list_library` passes `locking=False` and takes no locks.
    """
    stmt = (
        select(GameSession)
        .where(
            GameSession.game_id == game_id,
            GameSession.status.in_(("LOBBY", "IN_PROGRESS")),
        )
        .order_by(GameSession.created_at, GameSession.id)
    )
    if locking:
        stmt = fresh(stmt)
    sessions = (await db.execute(stmt)).scalars().all()
    live = [s for s in sessions if await state.get_room_state(redis, s.room_code)]

    host_ids = {s.host_user_id for s in live if s.host_user_id}
    names: dict[str, str | None] = {}
    if host_ids:
        rows = await db.execute(
            select(User.id, User.display_name, User.username, User.netid).where(
                User.id.in_(host_ids)
            )
        )
        names = {r.id: r.display_name or r.username or r.netid for r in rows}
    return [
        LiveSessionItem(
            session_id=s.id,
            room_code=s.room_code,
            status=s.status,
            host_name=names.get(s.host_user_id) if s.host_user_id else None,
            created_at=utc_z(s.created_at),
        )
        for s in live
    ]


def live_rooms_text(live: Sequence[LiveSessionItem]) -> str:
    """The live sessions, as every live-guard message lists them. Shared with T7's live
    freeze (game_management_service), which has its own lead-in."""
    return "; ".join(
        f"room {s.room_code} ({s.status}, hosted by {s.host_name or 'an unknown host'}, "
        f"created {s.created_at})"
        for s in live
    )


def _live_block_message(action: str, live: Sequence[LiveSessionItem]) -> str:
    rooms = live_rooms_text(live)
    return (
        f"Images can't be {action} while a session of this game is live: {rooms}. "
        "The session's host can rejoin and finish it, or delete it from Play (deleting "
        "discards its partial scores). Anyone else can ask that host or an admin; admins "
        "can delete sessions from Sessions."
    )


async def _assert_not_live(
    db: AsyncSession, redis: Redis, game_id: int, action: str
) -> None:
    live = await live_sessions(db, redis, game_id, locking=True)
    if live:
        raise ConflictError(_live_block_message(action, live))


# ── Export inputs, budgets and references (T8 spec §2.6, §2.7) ──────────────


def question_order(row: Any) -> tuple[int, int]:
    """The game's question order, (order_index, id), applied in Python.

    Reads that select question content never ORDER BY in SQL: MySQL 8's filesort carries the
    selected columns, `config` included, in its sort buffer (256 KB by default) and fails with
    error 1038 once one question's row is larger — a ~300 KB question is enough (T8 spec §4.6).
    """
    return (row.order_index, row.id)


@dataclass(frozen=True)
class ExportInputs:
    """A game loaded as plain records for `bundle_service`."""

    meta: GameMeta
    # (question id, order_index, fields), in the game's order (the editor's question numbers)
    questions: list[tuple[int, int, QuestionFields]]
    images: list[ImageRecord]  # by id

    @property
    def fields(self) -> list[QuestionFields]:
        return [fields for _, _, fields in self.questions]


async def export_inputs(
    db: AsyncSession, game_id: int, *, with_data: bool, locking: bool
) -> ExportInputs:
    """The game's metadata, questions and image records.

    Callers that check limits hold the game lock and pass `locking=True`: fresh shared reads
    (T8 spec §2.6). The game row itself is the object `lock_game` just refreshed. Export and
    `list_library` pass `locking=False`; only export passes `with_data=True`.
    """
    game = await db.get(Game, game_id)
    if game is None:
        raise NotFoundError(f"Game {game_id} not found")

    # No ORDER BY: see question_order.
    question_stmt = select(
        Question.id,
        Question.order_index,
        Question.type,
        Question.grading_type,
        Question.prompt,
        Question.config,
        Question.answer_data,
        Question.time_limit_seconds,
        Question.points_value,
    ).where(Question.game_id == game_id)
    # T7: the version and size too, for Spot It's confirmation rule in the bundle builder.
    columns: list[Any] = [
        Image.id,
        Image.filename,
        Image.description,
        Image.byte_size,
        Image.version,
        Image.width,
        Image.height,
    ]
    if with_data:
        columns.append(Image.data)
    # No ORDER BY here either: with `data` selected, a filesort would carry blobs of up to
    # 4 MiB, and whether MySQL avoids one depends on the plan it picks.
    image_stmt = select(*columns).where(Image.game_id == game_id)
    if locking:
        question_stmt, image_stmt = fresh(question_stmt), fresh(image_stmt)

    questions = [
        (row[0], row[1], QuestionFields(*row[2:]))
        for row in sorted((await db.execute(question_stmt)).all(), key=question_order)
    ]
    images = [
        ImageRecord(*row[:7], data=row[7] if with_data else None)
        for row in sorted((await db.execute(image_stmt)).all(), key=lambda row: row[0])
    ]
    return ExportInputs(
        meta=GameMeta(game.title, game.description, game.max_players),
        questions=questions,
        images=images,
    )


def check_export_change(
    inputs: ExportInputs,
    *,
    meta: GameMeta | None = None,
    questions: Sequence[QuestionFields] | None = None,
    images: Sequence[ImageRecord] | None = None,
) -> None:
    """The export rule on the game as it would be after a write: 409 if the export would grow
    past the export limit. Pass only what the write changes."""
    before = bundle_service.bundle_size(inputs.meta, inputs.fields, inputs.images)
    after = bundle_service.bundle_size(
        inputs.meta if meta is None else meta,
        inputs.fields if questions is None else questions,
        inputs.images if images is None else images,
    )
    bundle_service.check_export(before, after)


def check_budgets(count: int, library_bytes: int) -> None:
    """The image count and the library budget, on the would-be library → 409."""
    if count > IMAGE_COUNT_LIMIT:
        raise ConflictError(
            f"This game's library would have {count} images; "
            f"the limit is {IMAGE_COUNT_LIMIT}."
        )
    if library_bytes > LIBRARY_BUDGET_BYTES:
        raise ConflictError(
            f"This game's images would total {library_bytes:,} bytes "
            f"({_mib(library_bytes)} MiB); the library budget is "
            f"{LIBRARY_BUDGET_BYTES // MIB} MiB."
        )


def _not_in_library_message(missing: Sequence[int]) -> str:
    if len(missing) == 1:
        return f"Image {missing[0]} isn't in this game's library"
    return f"Images {', '.join(map(str, missing))} aren't in this game's library"


async def assert_in_library(db: AsyncSession, game_id: int, ids: Iterable[int]) -> None:
    """422 unless every id is an image of this game. The message is the same for a missing id
    and another game's id, so a host can't probe other courses' ids. A fresh shared read under
    the game lock, which serializes it with image delete."""
    wanted = {image_id for image_id in ids if is_image_id(image_id)}
    if not wanted:
        return
    found = set(
        (
            await db.execute(
                fresh(
                    select(Image.id).where(
                        Image.game_id == game_id, Image.id.in_(wanted)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(wanted - found)
    if missing:
        raise UnprocessableError(_not_in_library_message(missing))


def option_letter(index: int) -> str:
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def used_by_map(
    questions: Iterable[tuple[int, Any]], library_ids: set[int]
) -> dict[int, list[UsedBy]]:
    """Where each library image is used, from `(question id, config)` pairs in the game's
    order. References to ids outside the library are unresolved and don't count."""
    usage: dict[int, list[UsedBy]] = defaultdict(list)
    for number, (question_id, config) in enumerate(questions, start=1):
        for ref in image_refs(config):
            if ref.image_id in library_ids:
                usage[ref.image_id].append(
                    UsedBy(
                        question_id=question_id,
                        number=number,
                        role=ref.role,
                        option_letter=(
                            option_letter(ref.option_index)
                            if ref.option_index is not None
                            else None
                        ),
                    )
                )
    return usage


def describe_uses(uses: Iterable[UsedBy]) -> str:
    """The uses as the editor words them, e.g. "Q2 (prompt), Q5 (option B)"."""
    parts = []
    for use in uses:
        role = f"option {use.option_letter}" if use.role == OPTION else use.role
        parts.append(f"Q{use.number} ({role})")
    return ", ".join(parts)


# ── The library (T8 spec §2.8, §2.13) ───────────────────────────────────────


def image_item(image: Image, used_by: list[UsedBy], now: datetime) -> ImageItem:
    entry = link_entry(image, now)
    return ImageItem(
        id=image.id,
        filename=image.filename,
        description=image.description,
        content_type=image.content_type,
        width=image.width,
        height=image.height,
        byte_size=image.byte_size,
        version=image.version,
        replaced_at=utc_z(image.replaced_at) if image.replaced_at else None,
        created_at=utc_z(image.created_at),
        url=entry.url,
        small_url=entry.small_url,
        expires_at=entry.expires_at,
        used_by=used_by,
    )


async def _question_configs(db: AsyncSession, game_id: int) -> list[tuple[int, Any]]:
    rows = await db.execute(
        select(Question.id, Question.order_index, Question.config).where(
            Question.game_id == game_id
        )
    )
    return [(row[0], row[2]) for row in sorted(rows.all(), key=question_order)]


async def library_item(
    db: AsyncSession, game_id: int, image: Image, now: datetime
) -> ImageItem:
    """One image's item, as a mutation response, with its links and current uses."""
    usage = used_by_map(await _question_configs(db, game_id), {image.id})
    return image_item(image, usage.get(image.id, []), now)


async def list_library(
    db: AsyncSession, redis: Redis, game_id: int, now: datetime
) -> LibraryResponse:
    """The library, its live sessions and its meters. Takes no locks."""
    inputs = await export_inputs(db, game_id, with_data=False, locking=False)
    images = (
        (
            await db.execute(
                select(Image).where(Image.game_id == game_id).order_by(Image.id)
            )
        )
        .scalars()
        .all()
    )
    usage = used_by_map(
        ((qid, fields.config) for qid, _, fields in inputs.questions),
        {image.id for image in images},
    )
    return LibraryResponse(
        images=[image_item(image, usage.get(image.id, []), now) for image in images],
        live_sessions=await live_sessions(db, redis, game_id, locking=False),
        meters=LibraryMeters(
            image_count=len(images),
            image_limit=IMAGE_COUNT_LIMIT,
            library_bytes=sum(image.byte_size for image in images),
            library_budget=LIBRARY_BUDGET_BYTES,
            export_bytes=bundle_service.bundle_size(
                inputs.meta, inputs.fields, inputs.images
            ),
            export_limit=bundle_service.EXPORT_LIMIT_BYTES,
        ),
    )


async def precheck_library(db: AsyncSession, game_id: int) -> None:
    """Advisory, before any processing: 409 now if the library is already full, so a 51st
    upload isn't decoded first. A plain read; the locked checks in `add_image` decide."""
    count, library_bytes = (
        await db.execute(
            select(
                func.count(Image.id), func.coalesce(func.sum(Image.byte_size), 0)
            ).where(Image.game_id == game_id)
        )
    ).one()
    if count >= IMAGE_COUNT_LIMIT:
        check_budgets(count + 1, int(library_bytes))
    if int(library_bytes) >= LIBRARY_BUDGET_BYTES:
        raise ConflictError(
            f"This game's images already use the whole library budget "
            f"({LIBRARY_BUDGET_BYTES // MIB} MiB). Delete or replace an image first."
        )


def _stored_fields(processed: ProcessedImage) -> dict[str, Any]:
    return {
        "content_type": processed.content_type,
        "data": processed.data,
        "small_data": processed.small_data,
        "width": processed.width,
        "height": processed.height,
        "byte_size": processed.byte_size,
        "sha256": processed.sha256,
    }


async def _locked_image(db: AsyncSession, game_id: int, image_id: int) -> Image:
    """The image row being changed, read fresh and exclusively. Another game's image → 404."""
    image = (
        await db.execute(
            fresh(
                select(Image).where(Image.id == image_id, Image.game_id == game_id),
                exclusive=True,
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise NotFoundError(f"Image {image_id} not found in game {game_id}")
    return image


async def add_image(
    db: AsyncSession,
    game_id: int,
    processed: ProcessedImage,
    filename: str,
    description: str,
) -> Image:
    """Upload: the budgets and the export rule on the would-be library, then the insert."""
    await lock_game(db, game_id)
    inputs = await export_inputs(db, game_id, with_data=False, locking=True)
    check_budgets(
        len(inputs.images) + 1,
        sum(record.byte_size for record in inputs.images) + processed.byte_size,
    )
    # The new row gets the largest id (AUTO_INCREMENT), so it takes the last key in the bundle.
    pending = ImageRecord(
        id=max((record.id for record in inputs.images), default=0) + 1,
        filename=filename,
        description=description,
        byte_size=processed.byte_size,
        version=1,
        width=processed.width,
        height=processed.height,
        data=None,
    )
    check_export_change(inputs, images=[*inputs.images, pending])

    image = Image(
        game_id=game_id,
        filename=filename,
        description=description,
        version=1,
        **_stored_fields(processed),
    )
    db.add(image)
    await db.flush()
    await db.refresh(image, ["created_at", "updated_at", "replaced_at"])
    return image


async def replace_image(
    db: AsyncSession,
    redis: Redis,
    game_id: int,
    image_id: int,
    processed: ProcessedImage,
    filename: str,
) -> Image:
    """Replace the bytes: same id, version + 1, `replaced_at` = the database's NOW(), the new
    file's name; the description is kept. Refused while a session of the game is live."""
    await lock_game(db, game_id)
    image = await _locked_image(db, game_id, image_id)
    await _assert_not_live(db, redis, game_id, "replaced")
    inputs = await export_inputs(db, game_id, with_data=False, locking=True)
    check_budgets(
        len(inputs.images),
        sum(record.byte_size for record in inputs.images)
        - image.byte_size
        + processed.byte_size,
    )
    # T7: the substituted record carries the new version and size too — a replace can drop a
    # Spot It question's `"confirmed": true` from the export, and that must be measured.
    check_export_change(
        inputs,
        images=[
            record._replace(
                byte_size=processed.byte_size,
                filename=filename,
                version=record.version + 1,
                width=processed.width,
                height=processed.height,
            )
            if record.id == image_id
            else record
            for record in inputs.images
        ],
    )

    # Every check has passed: only now touch the row.
    for field, value in _stored_fields(processed).items():
        setattr(image, field, value)
    image.version = image.version + 1
    image.filename = filename
    image.replaced_at = func.now()
    await db.flush()
    await db.refresh(image, ["replaced_at", "updated_at"])
    return image


async def set_description(
    db: AsyncSession, game_id: int, image_id: int, description: str
) -> Image:
    """Edit the description (already cleaned). Allowed while a session is live."""
    await lock_game(db, game_id)
    image = await _locked_image(db, game_id, image_id)
    inputs = await export_inputs(db, game_id, with_data=False, locking=True)
    check_export_change(
        inputs,
        images=[
            record._replace(description=description)
            if record.id == image_id
            else record
            for record in inputs.images
        ],
    )
    image.description = description
    await db.flush()
    await db.refresh(image, ["updated_at"])
    return image


async def delete_image(
    db: AsyncSession, redis: Redis, game_id: int, image_id: int
) -> None:
    """Refused while a session is live, or while any question uses the image (409 listing the
    question numbers and roles)."""
    await lock_game(db, game_id)
    image = await _locked_image(db, game_id, image_id)
    await _assert_not_live(db, redis, game_id, "deleted")
    inputs = await export_inputs(db, game_id, with_data=False, locking=True)
    uses = used_by_map(
        ((qid, fields.config) for qid, _, fields in inputs.questions), {image_id}
    ).get(image_id)
    if uses:
        raise ConflictError(
            f"This image is in use, so it can't be deleted. Used by {describe_uses(uses)}."
        )
    await db.delete(image)
    await db.flush()


# ── Reading images for play, the report and T7 (T8 spec §2.10, §2.14, §2.16) ─


@dataclass(frozen=True)
class ImageMeta:
    id: int
    version: int
    width: int
    height: int
    description: str


async def get_image_meta(
    db: AsyncSession, game_id: int, image_id: int, *, locking: bool = False
) -> ImageMeta | None:
    """For T7's create, update, import and confirmation checks. Confirmation checks after the
    game lock must pass `locking=True` (a fresh shared read)."""
    stmt = select(
        Image.id, Image.version, Image.width, Image.height, Image.description
    ).where(Image.id == image_id, Image.game_id == game_id)
    if locking:
        stmt = fresh(stmt)
    row = (await db.execute(stmt)).one_or_none()
    return ImageMeta(*row) if row else None


async def images_for_refs(
    db: AsyncSession, game_id: int, ids: Iterable[int], *, with_small: bool = False
) -> dict[int, Image]:
    """The referenced images of this game, by id: one query, blobs only when asked. An id from
    another game is unresolved, exactly like a missing one."""
    wanted = {image_id for image_id in ids if is_image_id(image_id)}
    if not wanted:
        return {}
    stmt = select(Image).where(Image.game_id == game_id, Image.id.in_(wanted))
    if with_small:
        stmt = stmt.options(undefer(Image.small_data))
    return {image.id: image for image in (await db.execute(stmt)).scalars().all()}


def player_images(
    config: Any, images_by_id: dict[int, Image], now: datetime
) -> dict[str, dict[str, Any]]:
    """The phone's `images` map: option images only, small links. Prompt and canvas images
    never reach phones."""
    result: dict[str, dict[str, Any]] = {}
    for ref in image_refs(config):
        image = images_by_id.get(ref.image_id)
        if ref.role != OPTION or image is None or str(image.id) in result:
            continue
        url, _ = link(image, "small", now)
        result[str(image.id)] = {
            "url": url,
            "width": image.width,
            "height": image.height,
            "description": image.description,
        }
    return result


async def question_player_images(
    db: AsyncSession, question: Question, now: datetime
) -> dict[str, dict[str, Any]]:
    """`player_images` for one question, loading its option images in one query, scoped to
    the question's own game (another game's id stays unresolved)."""
    option_ids = [
        ref.image_id for ref in image_refs(question.config) if ref.role == OPTION
    ]
    images = await images_for_refs(db, question.game_id, option_ids)
    return player_images(question.config, images, now)


async def session_links(
    db: AsyncSession, session: GameSession, now: datetime
) -> SessionImages:
    """Full and small links for every image of the session's game (the projector's map)."""
    images = (
        (
            await db.execute(
                select(Image).where(Image.game_id == session.game_id).order_by(Image.id)
            )
        )
        .scalars()
        .all()
    )
    return SessionImages(
        images={str(image.id): link_entry(image, now) for image in images}
    )


def data_uri(image: Image, variant: str) -> str:
    """The image as a data URI; its blob must already be loaded (`images_for_refs`)."""
    if variant == "small":
        content_type, data = "image/webp", image.small_data
    else:
        content_type, data = image.content_type, image.data
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


# ── Bundle images on import (T8 spec §2.15 step 4) ──────────────────────────

# The longest base64 text that can decode to within the input limit.
_MAX_BASE64_CHARS = 4 * -(-INPUT_LIMIT_BYTES // 3)


def _schema_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"])
        parts.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(parts)


def check_bundle_entries(raw_entries: Sequence[Any]) -> list[BundleEntry]:
    """Every check an entry needs before decoding the image: exactly four fields, key rules,
    strict base64 within the input limit, the description rules. Returns the decoded bytes."""
    entries: list[BundleEntry] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_entries, start=1):
        key = raw.get("key") if isinstance(raw, dict) else None
        label = f"Image {key}" if isinstance(key, str) else f"Image entry {position}"
        try:
            item = BundleImage.model_validate(raw)
        except ValidationError as exc:
            raise UnprocessableError(f"{label}: {_schema_errors(exc)}") from exc
        label = f"Image {item.key}"
        if item.key in seen:
            raise UnprocessableError(
                f"{label}: this key is used by more than one image"
            )
        seen.add(item.key)
        if len(item.data) > _MAX_BASE64_CHARS:
            raise PayloadTooLargeError(
                f"{label}: {_over_limit_message(INPUT_LIMIT_BYTES)}"
            )
        try:
            raw_bytes = base64.b64decode(item.data, validate=True)
        except ValueError as exc:  # binascii.Error is a ValueError
            raise UnprocessableError(f"{label}: data isn't valid base64") from exc
        if len(raw_bytes) > INPUT_LIMIT_BYTES:
            raise PayloadTooLargeError(
                f"{label}: {_over_limit_message(INPUT_LIMIT_BYTES)}"
            )
        try:
            description = clean_description(item.description)
        except UnprocessableError as exc:
            raise UnprocessableError(f"{label}: {exc.message}") from exc
        entries.append(
            BundleEntry(
                key=item.key,
                filename=clean_filename(item.filename),
                description=description,
                raw=raw_bytes,
            )
        )
    return entries
