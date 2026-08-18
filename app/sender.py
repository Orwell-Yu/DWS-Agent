from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from .dws_listener import DwsError, DwsRunner
from .message_store import MessageStore
from .models import Task

LOGGER = logging.getLogger(__name__)
UUID_NAMESPACE = uuid.UUID("f2be5b4d-13ca-4f18-9fa8-912508779bbb")


class SendBlocked(RuntimeError):
    pass


class SelfReplyDetected(SendBlocked):
    pass


class MessageSender:
    def __init__(
        self,
        config: Dict[str, Any],
        dws: DwsRunner,
        store: MessageStore,
        self_reply_check: Callable[[Task], Awaitable[bool]],
    ):
        self.dws = dws
        self.store = store
        self.self_reply_check = self_reply_check
        self.update_config(config)

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = config

    def _allowed(self, task: Task) -> bool:
        safety = self.config["safety"]
        if not safety.get("send_enabled", False) or safety.get("paused", False):
            return False
        scope = safety.get("send_scope", "disabled")
        if scope == "all":
            return True
        if scope != "allowlist":
            return False
        if task.conversation_type == "private":
            return task.sender_id in {
                str(value) for value in safety.get("allowed_private_ids", [])
            }
        return task.conversation_id in {
            str(value) for value in safety.get("allowed_group_ids", [])
        }

    def is_allowed(self, task: Task) -> bool:
        return self._allowed(task)

    def _format_reply(self, task: Task, reply: str) -> str:
        reply = reply.strip()
        private_suffix = str(self.config["identity"].get("private_ai_suffix", "")).strip()
        group_suffix = str(self.config["identity"].get("group_ai_suffix", "")).strip()
        if task.conversation_type == "private":
            if private_suffix and not reply.endswith(private_suffix):
                reply = "%s\n\n%s" % (reply, private_suffix)
        elif group_suffix and not reply.endswith(group_suffix):
            reply = "%s\n\n%s" % (reply, group_suffix)
        return reply

    def send_uuid(self, task: Task) -> str:
        return str(uuid.uuid5(UUID_NAMESPACE, "%s:%s" % (task.task_id, task.message_id)))

    async def send(self, task: Task, reply: str) -> str:
        if self.config["safety"].get("reply_only") is not True:
            raise SendBlocked("reply-only safety invariant is not enabled")
        if not self._allowed(task):
            raise SendBlocked("real sending is disabled for this target")
        if not self.store.is_reply_task(task):
            raise SendBlocked("no verified inbound message exists to reply to")

        send_uuid = self.send_uuid(task)
        if self.store.send_status(send_uuid) == "sent":
            return send_uuid

        formatted = self._format_reply(task, reply)
        retry = self.config["retry"]
        attempts = int(retry.get("max_attempts", 3))
        delays = list(retry.get("delays_seconds", [2, 10, 30]))
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            if not self.store.is_reply_task(task):
                raise SendBlocked("reply task became stale or lost its inbound credential")
            try:
                if await self.self_reply_check(task):
                    raise SelfReplyDetected("a self reply was detected before send")
            except SendBlocked:
                raise
            except Exception as exc:
                self.store.upsert_send_log(send_uuid, task, attempt, "preflight_failed")
                raise SendBlocked("could not safely verify self-reply state") from exc

            if not self._allowed(task):
                raise SendBlocked("real sending was disabled during preflight")
            if not self.store.is_reply_task(task):
                raise SendBlocked("reply task became stale during preflight")

            args = [
                "chat",
                "message",
                "reply",
                "--conversation-id",
                task.conversation_id,
                "--ref-msg-id",
                task.message_id,
                "--ref-sender",
                task.sender_id,
                "--text",
                formatted,
                "--uuid",
                send_uuid,
                "--ai-tag=true",
            ]
            try:
                result = await self.dws.run_json(args, timeout=30)
                outbound_message_id = DwsRunner.extract_message_id(result)
                self.store.upsert_send_log(
                    send_uuid,
                    task,
                    attempt,
                    "sent",
                    outbound_message_id=outbound_message_id or None,
                )
                return send_uuid
            except DwsError as exc:
                last_error = exc
                self.store.upsert_send_log(send_uuid, task, attempt, "failed")
                LOGGER.warning(
                    "DWS send failed task_id=%s attempt=%s/%s",
                    task.task_id,
                    attempt,
                    attempts,
                )
                if attempt < attempts:
                    delay = float(delays[min(attempt - 1, len(delays) - 1)]) if delays else 2.0
                    await asyncio.sleep(delay)
        raise DwsError("message send failed after retries") from last_error
