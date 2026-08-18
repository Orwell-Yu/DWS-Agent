from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

from .models import Message


@dataclass
class ContextEntry:
    message_id: str
    sender_id: str
    sender_name: str
    received_at: float
    text: str
    content_type: str
    conversation_type: str = "private"


class ContextBuffer:
    def __init__(self, max_messages: int = 500):
        self.max_messages = max_messages
        self._conversations: Dict[str, Deque[ContextEntry]] = {}
        self._message_index: Dict[str, Message] = {}

    def add(self, message: Message) -> None:
        if not message.message_id:
            return
        if message.message_id in self._message_index:
            self._message_index[message.message_id] = message
            queue = self._conversations.get(message.conversation_id)
            if queue:
                for index, entry in enumerate(queue):
                    if entry.message_id == message.message_id:
                        queue[index] = ContextEntry(
                            message_id=message.message_id,
                            sender_id=message.sender_id,
                            sender_name=message.sender_name,
                            received_at=message.received_at,
                            text=message.text,
                            content_type=message.content_type,
                            conversation_type=message.conversation_type,
                        )
                        break
            return
        queue = self._conversations.setdefault(
            message.conversation_id, deque(maxlen=self.max_messages)
        )
        if len(queue) == queue.maxlen and queue:
            evicted = queue[0]
            self._message_index.pop(evicted.message_id, None)
        queue.append(
            ContextEntry(
                message_id=message.message_id,
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                received_at=message.received_at,
                text=message.text,
                content_type=message.content_type,
                conversation_type=message.conversation_type,
            )
        )
        self._message_index[message.message_id] = message

    def get_message(self, message_id: str) -> Optional[Message]:
        return self._message_index.get(message_id)

    def recent(
        self,
        conversation_id: str,
        limit: int,
        since: Optional[float] = None,
    ) -> List[ContextEntry]:
        values = list(self._conversations.get(conversation_id, ()))
        if since is not None:
            values = [item for item in values if item.received_at >= since]
        return values[-max(0, limit) :]

    def expire(
        self,
        group_ttl_seconds: float,
        private_ttl_seconds: float,
        now: Optional[float] = None,
    ) -> None:
        now = now or time.time()
        for conversation_id in list(self._conversations):
            queue = self._conversations[conversation_id]
            while queue:
                ttl = (
                    group_ttl_seconds
                    if queue[0].conversation_type == "group"
                    else private_ttl_seconds
                )
                if queue[0].received_at >= now - ttl:
                    break
                removed = queue.popleft()
                self._message_index.pop(removed.message_id, None)
            if not queue:
                del self._conversations[conversation_id]

    def clear(self) -> None:
        self._conversations.clear()
        self._message_index.clear()

    def stats(self) -> Dict[str, int]:
        return {
            "conversations": len(self._conversations),
            "messages": sum(len(queue) for queue in self._conversations.values()),
        }
