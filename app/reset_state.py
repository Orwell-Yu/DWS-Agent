from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from .config import parse_duration
from .message_store import MessageStore

LOGGER = logging.getLogger(__name__)


class ResetScheduler:
    def __init__(
        self,
        config: Dict[str, Any],
        store: MessageStore,
        clear_context: Callable[[], None],
    ):
        self.config = config
        self.store = store
        self.clear_context = clear_context
        self.stop_event = asyncio.Event()

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.config["timezone"])

    def current_boundary(self, now: Optional[datetime] = None) -> datetime:
        zone = self._zone()
        now = now or datetime.now(zone)
        reset = self.config["reset"]
        boundary = now.replace(
            hour=int(reset.get("hour", 4)),
            minute=int(reset.get("minute", 0)),
            second=0,
            microsecond=0,
        )
        if now < boundary:
            boundary -= timedelta(days=1)
        return boundary

    def next_boundary(self, now: Optional[datetime] = None) -> datetime:
        zone = self._zone()
        now = now or datetime.now(zone)
        boundary = self.current_boundary(now) + timedelta(days=1)
        return boundary

    def reset_now(self, timestamp: Optional[float] = None) -> int:
        now = timestamp or time.time()
        reset = self.config["reset"]
        generation = self.store.daily_reset(
            parse_duration(reset["seen_message_retention"]),
            parse_duration(reset["send_log_retention"]),
            now=now,
        )
        self.clear_context()
        LOGGER.info("local daily state reset complete generation=%s", generation)
        return generation

    def catch_up_if_needed(self) -> bool:
        boundary = self.current_boundary()
        last_value = self.store.get_meta("last_reset_at")
        last = float(last_value) if last_value else 0.0
        if last < boundary.timestamp():
            self.reset_now()
            return True
        return False

    async def run(self) -> None:
        self.catch_up_if_needed()
        while not self.stop_event.is_set():
            delay = max(0.1, self.next_boundary().timestamp() - time.time())
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                self.reset_now()

    def stop(self) -> None:
        self.stop_event.set()
