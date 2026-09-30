"""Game bundles: the one builder and serializer, the exact export size, and the export rule.

Pure: no database and no I/O. Callers load a game into the plain records below and pass them
in, so the exporter, the meters and every write's export check measure exactly the same thing.

- A game with an empty library exports as version 1, byte for byte as before T8, provided its
  configs hold no image keys (the export repairs below drop explicit nulls too).
- Otherwise it exports as version 2, with the whole library and questions referring to images
  by key (`img-1`, `img-2`, … in id order).
- T7: a Spot It question exports its portable form (`spot_it.export_fields`): no server-owned
  `width`/`height`, and `"confirmed": true` instead of the stamp, only when it is confirmed
  against the exported image. Measurement goes through the same builder, so the bytes measured
  are always the bytes exported.

Imports only image_refs, spot_it and common.exceptions (T8 spec §4.4; T7 spec §4.1).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any, NamedTuple

from ..common.exceptions import ConflictError
from . import spot_it
from .image_refs import CANVAS_KEY, OPTIONS_KEY, PROMPT_KEY, is_image_id, map_image_refs

MIB = 1024 * 1024
# The largest export a write may leave a game with (T8 spec §2.6).
EXPORT_LIMIT_BYTES = 40 * MIB
# The largest bundle file import accepts. Equal to the export limit, so every export re-imports.
IMPORT_LIMIT_BYTES = 40 * MIB

BUNDLE_FORMAT = "buzzer/game"
PLAIN_VERSION = 1
IMAGES_VERSION = 2
MISSING_IMAGE_CAPTION = "Missing image"


class GameMeta(NamedTuple):
    title: str
    description: str
    max_players: int


class QuestionFields(NamedTuple):
    """A question as stored, read from the database. Never build one from a request: MySQL
    can store a question longer than it was sent (see game_management_service._as_stored)."""

    type: str
    grading_type: str
    prompt: str
    config: Any
    answer_data: Any
    time_limit_seconds: int
    points_value: float


class ImageRecord(NamedTuple):
    id: int
    filename: str
    description: str
    byte_size: int
    # T7: the image's current version and size, for Spot It's "confirmed" (the shared
    # review rule). Required, so a constructor that forgets them fails loudly.
    version: int
    width: int
    height: int
    data: bytes | None  # the full image; None when only measuring


def serialize(bundle: dict) -> bytes:
    """The only bundle serializer, unchanged from the pre-T8 exporter."""
    return json.dumps(bundle, indent=2, ensure_ascii=False).encode()


def image_key(position: int) -> str:
    """The bundle key of the image at 1-based `position` in id order."""
    return f"img-{position}"


def _is_blank(caption: Any) -> bool:
    return caption is None or (isinstance(caption, str) and not caption.strip())


def _export_config(config: Any, key_for: Any) -> Any:
    """`config` with references mapped to keys, plus the export repairs (T8 spec §2.7).

    An unresolved reference is one `key_for` maps to None: a missing id, another game's id, or
    a value that isn't an id at all. The repairs make every such export re-import:
    - a null `prompt_image_id` or `image_id` is omitted
    - `option_images` is omitted when every entry is null; the question is then text-only
    - otherwise, a blank caption whose image was dropped becomes "Missing image", so every
      option keeps a caption or an image
    """
    if not isinstance(config, dict):
        return config
    mapped = map_image_refs(config, key_for)
    for key in (PROMPT_KEY, CANVAS_KEY):
        if key in mapped and mapped[key] is None:
            del mapped[key]
    if OPTIONS_KEY not in mapped:
        return mapped
    entries = mapped[OPTIONS_KEY]
    if entries is None or all(entry is None for entry in entries):
        del mapped[OPTIONS_KEY]
        return mapped
    options = mapped.get("options")
    if isinstance(options, list):
        captions = list(options)
        for index, (stored, exported) in enumerate(zip(config[OPTIONS_KEY], entries)):
            dropped = stored is not None and exported is None
            if dropped and index < len(captions) and _is_blank(captions[index]):
                captions[index] = MISSING_IMAGE_CAPTION
        mapped["options"] = captions
    return mapped


def _image_data(image: ImageRecord) -> str:
    if image.data is None:
        raise ValueError(f"Image {image.id} has no data loaded")
    return base64.b64encode(image.data).decode("ascii")


def build_bundle(
    meta: GameMeta,
    questions: Sequence[QuestionFields],
    images: Sequence[ImageRecord],
    *,
    include_data: bool,
) -> dict:
    """The bundle for a game, in the T8 spec §2.15 format.

    With `include_data=False` every image's `data` is `""`, which `bundle_size` measures.
    """
    ordered = sorted(images, key=lambda image: image.id)
    keys = {image.id: image_key(n) for n, image in enumerate(ordered, start=1)}
    infos = {
        image.id: spot_it.ImageInfo(image.id, image.version, image.width, image.height)
        for image in ordered
    }

    def key_for(value: Any) -> str | None:
        return keys.get(value) if is_image_id(value) else None

    def portable(q: QuestionFields) -> tuple[Any, Any]:
        """(config, answer_data) as exported, before references are mapped to keys."""
        if q.type != spot_it.TYPE or not isinstance(q.config, dict):
            return q.config, q.answer_data
        image_id = q.config.get(CANVAS_KEY)
        info = infos.get(image_id) if is_image_id(image_id) else None
        return spot_it.export_fields(q.config, q.answer_data, info)

    bundle: dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "version": IMAGES_VERSION if ordered else PLAIN_VERSION,
        "game": {
            "title": meta.title,
            "description": meta.description,
            "max_players": meta.max_players,
        },
    }
    if ordered:
        bundle["images"] = [
            {
                "key": keys[image.id],
                "filename": image.filename,
                "description": image.description,
                "data": _image_data(image) if include_data else "",
            }
            for image in ordered
        ]
    exported = []
    for q in questions:
        config, answer_data = portable(q)
        exported.append(
            {
                "type": q.type,
                "grading_type": q.grading_type,
                "prompt": q.prompt,
                "config": _export_config(config, key_for),
                "answer_data": answer_data,
                "time_limit_seconds": q.time_limit_seconds,
                "points_value": q.points_value,
            }
        )
    bundle["questions"] = exported
    return bundle


def bundle_size(
    meta: GameMeta,
    questions: Sequence[QuestionFields],
    images: Sequence[ImageRecord],
) -> int:
    """The exact byte length of the exported bundle, without building any base64.

    Exact because base64 characters need no JSON escaping: each image adds 4·⌈n/3⌉ characters
    inside the `""` already serialized. Never raises on malformed references.
    """
    skeleton = serialize(build_bundle(meta, questions, images, include_data=False))
    return len(skeleton) + sum(4 * ((image.byte_size + 2) // 3) for image in images)


def check_export(before: int, after: int) -> None:
    """The export rule: accept a write if the export stays within the limit or doesn't grow.

    "Doesn't grow" lets a game that is already over the limit (possible only before T8) be
    edited and shrunk.
    """
    if after > EXPORT_LIMIT_BYTES and after > before:
        raise ConflictError(
            f"This change would make the game's export {after:,} bytes, over the export "
            f"limit of {EXPORT_LIMIT_BYTES:,} bytes (40 MiB). It is {before:,} bytes now."
        )
