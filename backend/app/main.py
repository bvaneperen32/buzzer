from contextlib import asynccontextmanager

import socketio as _socketio
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .common.exceptions import register_exception_handlers
from .common.logging import configure_logging
from .common.rate_limit import limiter
from .config import settings
from .redis_client import close_redis, get_redis
from .routers import admin, auth, game, health, host
from .services.bootstrap import bootstrap_admin
from .websocket.gateway import sio as _ws_server

configure_logging()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", env=settings.APP_ENV)
    await get_redis()
    await bootstrap_admin()
    yield
    logger.info("shutdown")
    await close_redis()


app = FastAPI(
    title="Buzzer API",
    version="1.0.0",
    docs_url="/api/docs" if settings.is_development else None,
    redoc_url="/api/redoc" if settings.is_development else None,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(game.router, prefix="/api")
app.include_router(host.router, prefix="/api")

# Wrap FastAPI with the socketio ASGI app.
# Requests to /socket.io/* are handled by python-socketio; everything else
# falls through to the FastAPI app.  This outer-wrap pattern is required
# because Starlette's Mount strips the path prefix before the sub-app sees it,
# which breaks socketio's own path matching.
asgi_app = _socketio.ASGIApp(_ws_server, other_asgi_app=app)
