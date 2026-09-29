"""The game row lock and fresh reads.

Every write that checks a game's limits or references, and room creation and game start, first
calls `lock_game`, so writes to one game run one at a time until the transaction ends.

Reads made after the lock whose results feed a check, a merge, a version increment or a start
must use `fresh(...)`, for two separate reasons:

- MySQL's REPEATABLE READ: a plain read reuses the snapshot taken at the transaction's first
  read (usually the access check, before the lock wait), so it misses writes committed during
  the wait. A locking read always sees the latest committed rows.
- SQLAlchemy's identity map: a row already loaded in the session comes back as the existing
  object, unchanged, unless the query sets `populate_existing`. Sessions use
  `expire_on_commit=False`, so nothing else refreshes them.

`populate_existing` also overwrites changes that haven't been flushed, so take every locking
read first, check plain values, and only then assign to ORM objects.

Imports only models and common.exceptions (see the module dependencies in the T8 spec, §4.4).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy import Select, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.exceptions import ConflictError, NotFoundError
from ..models.game import Game

MYSQL_LOCK_WAIT_TIMEOUT = 1205
MYSQL_DEADLOCK = 1213
LOCK_CONFLICT_MESSAGE = "This game is being changed by another request; try again."

SelectT = TypeVar("SelectT", bound=Select)


def fresh(stmt: SelectT, *, exclusive: bool = False) -> SelectT:
    """A locking read with `populate_existing`.

    Shared (`LOCK IN SHARE MODE` on MySQL) by default: it still sees the latest committed rows,
    and doesn't conflict with the shared locks that answer inserts take on question rows.
    `exclusive=True` (`FOR UPDATE`) is only for the rows being changed.
    """
    return stmt.with_for_update(read=not exclusive).execution_options(
        populate_existing=True
    )


def _mysql_error_code(exc: DBAPIError) -> int | None:
    args = getattr(exc.orig, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


@contextmanager
def lock_conflicts() -> Iterator[None]:
    """Turn MySQL's lock-wait timeout and deadlock into the 409 the spec defines.

    The request's transaction then rolls back in `get_db`, so nothing is changed.
    """
    try:
        yield
    except DBAPIError as exc:
        if _mysql_error_code(exc) in (MYSQL_LOCK_WAIT_TIMEOUT, MYSQL_DEADLOCK):
            raise ConflictError(LOCK_CONFLICT_MESSAGE) from exc
        raise


async def lock_game(db: AsyncSession, game_id: int) -> Game:
    """`SELECT … FROM games WHERE id = :id FOR UPDATE`, held until the transaction ends.

    Returns the refreshed `Game`; if the session already holds that object (from the access
    check), it is the same object, now with the latest committed values.
    """
    with lock_conflicts():
        result = await db.execute(
            fresh(select(Game).where(Game.id == game_id), exclusive=True)
        )
    game = result.scalar_one_or_none()
    if game is None:
        raise NotFoundError(f"Game {game_id} not found")
    return game
