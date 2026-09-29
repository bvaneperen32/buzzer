"""The only definition of which question `config` keys hold image ids, and their roles.

| key               | role     | shape                                          |
|-------------------|----------|------------------------------------------------|
| `prompt_image_id` | `prompt` | int, or absent/null                            |
| `option_images`   | `option` | list parallel to `options`; each int or null   |
| `image_id`        | `canvas` | int (canvas question types only, from T7)      |

Pure: no database, no I/O, no imports from the application. Every reader of references goes
through here, so the library check, `used_by`, the delete refusal and both bundle directions
already handle all three roles. T7 changes only `validate_structure`, to allow `image_id` on its
canvas type.

Stored configs can hold anything (`config` accepted extra keys before T8), so nothing here
raises on malformed values.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

PROMPT_KEY = "prompt_image_id"
OPTIONS_KEY = "option_images"
CANVAS_KEY = "image_id"

PROMPT = "prompt"
OPTION = "option"
CANVAS = "canvas"


class Ref(NamedTuple):
    image_id: int
    role: str  # PROMPT, OPTION or CANVAS
    option_index: int | None  # the position in `options`, for OPTION only


def is_image_id(value: Any) -> bool:
    """A positive int. Bools are ints in Python, and `True` would otherwise read as image 1."""
    return type(value) is int and value > 0


def image_refs(config: Any) -> list[Ref]:
    """Every image id `config` refers to, in the order prompt, options, canvas.

    Returns only positive ints; any other value is ignored. Callers then drop ids that aren't
    in the question's own game library, so an unresolved reference counts as no image.
    """
    if not isinstance(config, dict):
        return []
    refs: list[Ref] = []
    if is_image_id(value := config.get(PROMPT_KEY)):
        refs.append(Ref(value, PROMPT, None))
    entries = config.get(OPTIONS_KEY)
    if isinstance(entries, list):
        refs.extend(
            Ref(value, OPTION, index)
            for index, value in enumerate(entries)
            if is_image_id(value)
        )
    if is_image_id(value := config.get(CANVAS_KEY)):
        refs.append(Ref(value, CANVAS, None))
    return refs


def map_image_refs(config: Any, fn: Callable[[Any], Any]) -> Any:
    """A copy of `config` with every reference slot replaced by `fn(value)`, whatever the value.

    Nulls stay null and absent keys stay absent; a non-list `option_images` becomes `None`.
    Other keys keep their values and their order. Slots are visited in `image_refs` order.
    `fn` may raise (import refuses unknown keys); this function itself never does. A non-dict
    `config` is returned unchanged.
    """
    if not isinstance(config, dict):
        return config
    mapped = dict(config)
    if mapped.get(PROMPT_KEY) is not None:
        mapped[PROMPT_KEY] = fn(mapped[PROMPT_KEY])
    if OPTIONS_KEY in mapped:
        entries = mapped[OPTIONS_KEY]
        mapped[OPTIONS_KEY] = (
            [None if value is None else fn(value) for value in entries]
            if isinstance(entries, list)
            else None
        )
    if mapped.get(CANVAS_KEY) is not None:
        mapped[CANVAS_KEY] = fn(mapped[CANVAS_KEY])
    return mapped
