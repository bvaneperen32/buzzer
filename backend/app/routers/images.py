"""
/api/images/* — serving image bytes through signed links (T8 spec §2.9).

No bearer token and no auth dependency: an `<img>` element or a canvas load can't send the
header. The signed `t` query parameter is the whole check. It is created only by
`image_service` at points where access has already been checked, and it names one image,
version and variant. So this endpoint returns 403 (bad token) or 404 (gone or superseded),
never 401.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services import image_service

router = APIRouter(prefix="/images", tags=["images"])


@router.get("/{image_id}/v{version}/{variant}")
async def serve_image(
    image_id: int,
    version: int,
    variant: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    t: Annotated[str, Query(description="The signed link token")] = "",
) -> Response:
    expires = image_service.verify_link(image_id, version, variant, t)
    data, content_type = await image_service.read_image_bytes(
        db, image_id, version, variant
    )
    # End the read transaction now, returning the pooled connection before the body is sent.
    # get_db would only commit after the response, so every phone on slow Wi-Fi would hold a
    # connection — shared with answer handling — for as long as its download takes.
    await db.commit()
    # A version's bytes never change (a replace makes a new version, so a new URL), so the
    # link can be cached until it expires. Error responses get no caching headers.
    max_age = max(0, expires - int(time.time()))
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": f"private, max-age={max_age}, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
