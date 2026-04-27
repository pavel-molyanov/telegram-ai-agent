"""Short-lived in-memory store for Telegram /resume picker callbacks."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.services.resume_listing import SessionEntry
from telegram_bot.core.services.topic_config import Engine


@dataclass(frozen=True)
class PickerState:
    chat_id: int
    thread_id: int | None
    cwd: Path
    engine: Engine
    entries: tuple[SessionEntry, ...]
    created_at: float


class PickerStore:
    """In-memory picker state. TTL is enforced lazily on access."""

    def __init__(self, *, ttl_sec: float = 300.0, clock: object = time.time) -> None:
        self._ttl_sec = ttl_sec
        self._clock = clock
        self._states: dict[str, PickerState] = {}

    def put(self, state: PickerState) -> str:
        while True:
            token = secrets.token_hex(4)
            if token not in self._states:
                self._states[token] = state
                return token

    def get(self, token: str) -> PickerState | None:
        state = self._states.get(token)
        if state is None:
            return None
        now = self._clock()  # type: ignore[operator]
        if now - state.created_at > self._ttl_sec:
            self._states.pop(token, None)
            return None
        return state

    def drop(self, token: str) -> None:
        self._states.pop(token, None)
