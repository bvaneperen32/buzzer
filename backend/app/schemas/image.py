"""Image library and link contracts (T8), plus the strict shape of a bundle image.

REST fields are snake_case. Timestamps are ISO 8601 UTC strings with a `Z` and whole seconds,
formatted by `image_service`. Every `width`/`height` is the full image's size; the small copy has
the same aspect ratio.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UsedBy(BaseModel):
    question_id: int
    number: int  # the question's 1-based position in the game's current order
    role: Literal["prompt", "option", "canvas"]
    option_letter: str | None = None  # for "option" only


class ImageItem(BaseModel):
    id: int
    filename: str
    description: str
    content_type: str
    width: int
    height: int
    byte_size: int
    version: int
    replaced_at: str | None
    created_at: str
    url: str
    small_url: str
    expires_at: str
    used_by: list[UsedBy]


class LiveSessionItem(BaseModel):
    session_id: str
    room_code: str
    status: Literal["LOBBY", "IN_PROGRESS"]
    host_name: str | None
    created_at: str


class LibraryMeters(BaseModel):
    image_count: int
    image_limit: int
    library_bytes: int
    library_budget: int
    export_bytes: int
    export_limit: int


class LibraryResponse(BaseModel):
    images: list[ImageItem]  # ordered by id
    live_sessions: list[LiveSessionItem]
    meters: LibraryMeters


class LinkEntry(BaseModel):
    url: str
    small_url: str
    width: int
    height: int
    description: str
    expires_at: str


class SessionImages(BaseModel):
    images: dict[str, LinkEntry]  # keyed by image id as a string


class DescriptionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Trimmed and length-checked by image_service.clean_description, like an upload's.
    description: str


class BundleImage(BaseModel):
    """One entry of a version 2 bundle's `images`: exactly these four fields."""

    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    filename: str
    description: str
    data: str  # strict base64 of the image bytes
