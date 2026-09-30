"""
Async socket.io client wrapper for test orchestration.

Each instance wraps one python-socketio.AsyncClient and exposes a simple
`wait_for(event)` API backed by per-event asyncio.Queue objects so that
multiple events of the same type can be queued and consumed in order.
"""
from __future__ import annotations

import asyncio
import logging

import socketio

logger = logging.getLogger(__name__)

# Events the orchestrator cares about
_TRACKED = [
    "sync_state",
    "new_question",
    "question_results",
    "game_over",
    "answer_received",
    "answer_status",
    "answer_phase_ended",
    "player_joined",
    "player_left",
    "host_disconnected",
    "game_abandoned",
    "question_locked",
    "question_unlocked",
    "host_reconnected",
    "answer_rejected",
    "results_delayed",
    "error",
]


class TestSocketClient:
    """One authenticated socket.io connection used in integration tests."""

    def __init__(self, base_url: str, token: str, label: str = ""):
        self.base_url = base_url
        self.token = token
        self.label = label
        self.sio = socketio.AsyncClient(logger=False, engineio_logger=False)
        self._queues: dict[str, asyncio.Queue] = {e: asyncio.Queue() for e in _TRACKED}
        # Every tracked event's name, in the order it arrived.
        self.order: list[str] = []
        self._register_handlers()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _register_handlers(self):
        for event in _TRACKED:
            # Capture event name in default argument to avoid late-binding issue
            @self.sio.on(event)
            async def _handler(data=None, _ev=event):
                logger.debug("[%s] received %s: %s", self.label, _ev, data)
                self.order.append(_ev)
                await self._queues[_ev].put(data or {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self):
        await self.sio.connect(
            self.base_url,
            socketio_path="/socket.io",
            auth={"token": self.token},
            transports=["websocket"],
        )
        logger.info("[%s] connected", self.label)

    async def disconnect(self):
        if self.sio.connected:
            await self.sio.disconnect()
            logger.info("[%s] disconnected", self.label)

    async def emit(self, event: str, data: dict):
        logger.debug("[%s] emit %s: %s", self.label, event, data)
        await self.sio.emit(event, data)

    async def wait_for(self, event: str, timeout: float = 10.0) -> dict:
        """
        Block until the next occurrence of `event` arrives, then return its payload.
        Raises asyncio.TimeoutError if it does not arrive within `timeout` seconds.
        """
        payload = await asyncio.wait_for(self._queues[event].get(), timeout=timeout)
        logger.debug("[%s] consumed %s: %s", self.label, event, payload)
        return payload

    def drain(self, *events: str):
        """Discard any already-queued payloads for the given events."""
        for ev in events:
            q = self._queues.get(ev)
            if q:
                while not q.empty():
                    q.get_nowait()
