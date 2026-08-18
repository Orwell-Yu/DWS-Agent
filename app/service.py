from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .admin_server import AdminServer
from .config import ConfigManager, group_ids, parse_duration
from .context import ContextBuffer, ContextEntry
from .decision import DecisionEngine, is_special_care
from .dws_listener import DwsError, DwsListenerManager, DwsRunner
from .message_store import MessageStore
from .models import Message, Task, normalize_event
from .reaction import select_group_reaction, select_text_emotion
from .repository_reader import RepositoryReader
from .reset_state import ResetScheduler
from .resource_reader import ResourceReader
from .responder import CodexResponder, ResponderError
from .sender import MessageSender, SelfReplyDetected, SendBlocked

LOGGER = logging.getLogger(__name__)
COUNT_RE = re.compile(r"(?:最近|前|读取|看)(\d{1,4})\s*条")
TIME_RE = re.compile(r"(?:最近|过去|前)(\d{1,4})\s*(分钟|小时|天)")


class AutoReplyService:
    def __init__(self, config_path: str):
        self.config_manager = ConfigManager(config_path)
        self.config = self.config_manager.get()
        project = Path(config_path).resolve().parent
        self.store = MessageStore(str(project / "state.sqlite3"))
        self.context = ContextBuffer(max_messages=int(self.config["context"]["max_messages"]))
        self.decisions = DecisionEngine()
        self.dws = DwsRunner(self.config)
        self.repository_reader = RepositoryReader(self.config)
        self.resources = ResourceReader(self.config, self.dws)
        self.responder = CodexResponder(self.config)
        self.sender = MessageSender(self.config, self.dws, self.store, self._has_self_reply)
        self.resetter = ResetScheduler(self.config, self.store, self.context.clear)
        self.listener = DwsListenerManager(self.config, self.on_message)
        self.admin = AdminServer(
            self.config_manager,
            self.status,
            self.request_reload,
            self.request_restart,
        )
        self.stop_event = asyncio.Event()
        self.reload_event = asyncio.Event()
        self.restart_requested = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._background: List[asyncio.Task] = []
        self._started_at = time.time()

    @staticmethod
    def _listener_signature(config: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            config["dws"]["binary"],
            config["dws"]["config_dir"],
            config["dws"]["profile"],
            tuple(
                config["dws"]["event_keys"][name]
                for name in ("private", "at", "group")
            ),
            tuple(sorted(group_ids(config))),
        )

    def request_reload(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.reload_event.set)

    def request_restart(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._restart)

    def _restart(self) -> None:
        self.restart_requested = True
        self.stop_event.set()

    def status(self) -> Dict[str, Any]:
        config = self.config_manager.get()
        return {
            "safety": {
                "send_enabled": bool(config["safety"].get("send_enabled")),
                "send_scope": config["safety"].get("send_scope"),
                "paused": bool(config["safety"].get("paused", False)),
                "reply_only": config["safety"].get("reply_only") is True,
            },
            "group_reaction": {
                "enabled": bool(config["group_reaction"].get("enabled")),
                "strategy": config["group_reaction"].get("strategy"),
                "mode": config["group_reaction"].get("mode", "emoji"),
                "fallback_emoji": config["group_reaction"].get("fallback_emoji"),
                "text_emotion_count": len(
                    config["group_reaction"].get("text_emotions", [])
                ),
            },
            "groups": {
                "mode": config["groups"].get("mode", "all"),
                "context_listener_count": len(config["groups"].get("whitelist", [])),
                "immediate_reply": [
                    group["name"]
                    for group in config["groups"].get("whitelist", [])
                    if group.get("immediate_reply", False)
                ],
            },
            "listeners": self.listener.status(),
            "store": self.store.status_summary(),
            "context": self.context.stats(),
            "recent_tasks": self.store.recent_tasks(50),
        }

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.resetter.catch_up_if_needed()
        recovery = self.store.recover(
            parse_duration(self.config["reset"]["restart_task_max_lateness"])
        )
        LOGGER.info("task recovery stale=%s recovered=%s", recovery["stale"], recovery["recovered"])
        await self.listener.start()
        if self.config["web"].get("enabled", True):
            self.admin.start(self.config["web"]["host"], int(self.config["web"]["port"]))
        self._background = [
            asyncio.create_task(self._task_loop(), name="task-loop"),
            asyncio.create_task(self.resetter.run(), name="reset-loop"),
            asyncio.create_task(self._reload_loop(), name="reload-loop"),
            asyncio.create_task(self._context_expiry_loop(), name="context-expiry"),
            asyncio.create_task(
                self._private_recovery_loop(), name="private-recovery"
            ),
        ]
        LOGGER.info(
            "service started send_enabled=%s scope=%s",
            self.config["safety"].get("send_enabled"),
            self.config["safety"].get("send_scope"),
        )

    async def run(self) -> None:
        await self.start()
        await self.stop_event.wait()
        await self.shutdown()

    def stop(self) -> None:
        self.stop_event.set()

    async def shutdown(self) -> None:
        LOGGER.info("service shutdown started")
        self.resetter.stop()
        await self.listener.stop()
        await self.admin.stop()
        for task in self._background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        self.store.close()
        LOGGER.info("service shutdown complete")

    async def on_message(self, message: Message) -> None:
        if not message.event_id or not message.message_id or not message.conversation_id:
            LOGGER.warning("ignored event with missing stable identifiers source=%s", message.source)
            return
        if not self.store.record_seen(message):
            return

        own_ids = {
            str(self.config["identity"].get("user_id", "")),
            str(self.config["identity"].get("open_dingtalk_id", "")),
        }
        if message.sender_id in own_ids:
            cancelled = self.store.cancel_for_self_message(
                message.conversation_id,
                message.received_at,
                message.conversation_type,
            )
            LOGGER.info(
                "self message observed conversation_type=%s cancelled=%s",
                message.conversation_type,
                cancelled,
            )
            return

        if message.source == "group_context":
            supported = set(self.config["content"].get("supported_types", []))
            if message.content_type in supported:
                self.context.add(message)
            return

        # The daily limit is enforced again after the DWS self-reply preflight.
        # Use zero here so a manual reply made since the last auto-reply can
        # reset a previously exhausted counter before the task is discarded.
        preliminary = self.decisions.decide(message, self.config, 0)
        if preliminary.action == "ignore":
            LOGGER.info(
                "event ignored source=%s type=%s reason=%s message_id=%s",
                message.source,
                message.content_type,
                preliminary.reason,
                message.message_id,
            )
            return

        if message.conversation_type == "group" and message.source == "at":
            await self._react_to_group_mention(message)

        self.context.add(message)
        special_user = is_special_care(message, self.config)
        immediate_group = message.conversation_type == "group" and any(
            group["conversation_id"] == message.conversation_id
            and group.get("immediate_reply", False)
            for group in self.config["groups"].get("whitelist", [])
        )
        immediate = special_user or immediate_group
        if immediate:
            delay = 0.0
        elif message.conversation_type == "private":
            delay = parse_duration(self.config["private_chat"]["delay"])
        else:
            delay = parse_duration(self.config["groups"]["delay"])
        due_at = message.received_at + delay
        task_id = self.store.schedule(message, due_at, immediate)
        LOGGER.info(
            "task scheduled task_id=%s conversation_type=%s immediate=%s due_in=%.1fs",
            task_id,
            message.conversation_type,
            immediate,
            max(0.0, due_at - time.time()),
        )

    def _group_reaction_allowed(self, message: Message) -> bool:
        if not self.config["group_reaction"].get("enabled", False):
            return False
        safety = self.config["safety"]
        if not safety.get("send_enabled", False) or safety.get("paused", False):
            return False
        scope = safety.get("send_scope", "disabled")
        if scope == "all":
            return True
        if scope != "allowlist":
            return False
        return message.conversation_id in {
            str(value) for value in safety.get("allowed_group_ids", [])
        }

    async def _react_to_group_mention(self, message: Message) -> None:
        if not self._group_reaction_allowed(message):
            return
        section = self.config["group_reaction"]
        if section.get("mode", "emoji") == "text_emotion":
            selected = select_text_emotion(
                message.text,
                fallback=str(
                    section.get("fallback_text_emotion", "收到，我认真看看")
                ).strip(),
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                string_sender_ids=section.get("string_sender_ids", []),
                string_sender_names=section.get("string_sender_names", []),
                targeted_emotion=str(
                    section.get("targeted_text_emotion", "特别回应")
                ).strip(),
            )
            emotions = {
                str(item["name"]): item for item in section.get("text_emotions", [])
            }
            emotion = emotions.get(selected)
            if emotion is None:
                LOGGER.warning("configured text emotion not found name=%s", selected)
                return
            args = [
                "chat",
                "message",
                "add-text-emotion",
                "--conversation-id",
                message.conversation_id,
                "--msg-id",
                message.message_id,
                "--emotion-id",
                str(emotion["emotion_id"]),
                "--emotion-name",
                str(emotion["name"]),
                "--text",
                str(emotion["text"]),
                "--background-id",
                str(emotion["background_id"]),
            ]
            label = str(emotion["name"])
        else:
            emoji = select_group_reaction(
                message.text,
                fallback=str(section.get("fallback_emoji", "收到")).strip(),
            )
            args = [
                "chat",
                "message",
                "add-emoji",
                "--conversation-id",
                message.conversation_id,
                "--msg-id",
                message.message_id,
                "--emoji",
                emoji,
            ]
            label = emoji
        try:
            await self.dws.run_json(
                args,
                timeout=float(section.get("timeout_seconds", 10)),
            )
            LOGGER.info(
                "group mention reaction added message_id=%s reaction=%s",
                message.message_id,
                label,
            )
        except DwsError:
            LOGGER.warning(
                "group mention reaction failed message_id=%s reaction=%s",
                message.message_id,
                label,
            )

    async def _has_self_reply(self, task: Task) -> bool:
        items = await self.dws.self_messages_since(task)
        if not (task.special_care and task.conversation_type == "private"):
            return bool(items)
        markers = self.store.sent_markers(task.conversation_id, task.received_at)
        auto_ids = markers["message_ids"]
        auto_times = markers["attempted_at"]
        for item in items:
            message_id = self.dws.history_item_message_id(item)
            timestamp = self.dws.history_item_timestamp(item)
            if message_id and message_id in auto_ids:
                continue
            if (
                not message_id or not auto_ids
            ) and any(abs(timestamp - attempted_at) <= 15 for attempted_at in auto_times):
                continue
            return True
        return False

    def _context_request(self, text: str) -> Tuple[int, float, bool]:
        section = self.config["context"]
        default_limit = int(section["default_messages"])
        max_limit = int(section["max_messages"])
        max_age = parse_duration(section["max_age"])
        explicit = False
        limit = default_limit
        age = max_age
        count = COUNT_RE.search(text)
        if count:
            limit = min(max_limit, max(1, int(count.group(1))))
            explicit = True
        period = TIME_RE.search(text)
        if period:
            multiplier = {"分钟": 60, "小时": 3600, "天": 86400}[period.group(2)]
            age = min(max_age, max(60, int(period.group(1)) * multiplier))
            explicit = True
            if not count:
                limit = max_limit
        return limit, age, explicit

    def _history_context(self, items: List[Dict[str, Any]]) -> List[ContextEntry]:
        result = []
        seen = set()
        for item in items:
            message_id = self.dws.history_item_message_id(item)
            sender_id = self.dws.history_item_sender(item)
            text = self.dws.history_item_text(item)
            timestamp = self.dws.history_item_timestamp(item)
            key = message_id or (sender_id, timestamp, text)
            if key in seen or not text:
                continue
            seen.add(key)
            result.append(
                ContextEntry(
                    message_id=message_id,
                    sender_id=sender_id,
                    sender_name=self.dws.history_item_name(item),
                    received_at=timestamp,
                    text=text,
                    content_type="text",
                    conversation_type="history",
                )
            )
        result.sort(key=lambda value: value.received_at)
        return result

    async def _build_context(self, task: Task, message: Message) -> List[ContextEntry]:
        limit, age, explicit = self._context_request(message.text)
        since = time.time() - age
        in_memory = self.context.recent(task.conversation_id, limit, since=since)
        if len(in_memory) >= limit and not explicit:
            return in_memory[-limit:]
        try:
            if explicit:
                history = await self.dws.fetch_history(
                    task.conversation_type,
                    task.conversation_id,
                    task.sender_id,
                    since,
                    limit,
                    direction="newer",
                )
            else:
                history = await self.dws.fetch_history(
                    task.conversation_type,
                    task.conversation_id,
                    task.sender_id,
                    time.time(),
                    limit,
                    direction="older",
                )
            values = self._history_context(history)
            return values[-limit:] if values else in_memory[-limit:]
        except DwsError:
            LOGGER.warning("history context unavailable task_id=%s", task.task_id)
            return in_memory[-limit:]

    async def _process_task(self, task: Task) -> None:
        if not self.store.claim(task.task_id, task.generation):
            return

        send_allowed = self.sender.is_allowed(task)
        monitor_generate = bool(self.config["safety"].get("monitor_only_generate_ai", False))
        if not send_allowed and not monitor_generate:
            self.store.set_status(task.task_id, "monitor_only")
            LOGGER.info("monitor-only task observed task_id=%s", task.task_id)
            return

        message = self.context.get_message(task.last_user_message_id)
        if message is None:
            try:
                message = await self.dws.recover_message(task)
            except DwsError:
                message = None
        if message is None:
            self.store.set_status(task.task_id, "cancelled:missing_context")
            return

        try:
            already_replied = await self._has_self_reply(task)
        except DwsError:
            self.store.set_status(task.task_id, "cancelled:preflight_unavailable")
            return
        if already_replied:
            if task.conversation_type == "private":
                self.store.reset_reply_count(task.sender_id)
            self.store.set_status(task.task_id, "cancelled:self_reply")
            return

        current_count = self.store.reply_count(task.sender_id)
        decision = self.decisions.decide(message, self.config, current_count, False)
        if decision.action == "ignore":
            self.store.set_status(task.task_id, "ignored:%s" % decision.reason)
            return

        context = await self._build_context(task, message)
        external_context = ""
        code_context = ""
        image_paths: List[str] = []
        try:
            code_question = decision.category in {"code", "code_summary"}
            if message.urls and not code_question:
                external_context = await self.resources.read_links(message.urls)
            if message.content_type == "image" and not code_question:
                try:
                    image_paths = await self.resources.download_images(message)
                except Exception:
                    LOGGER.warning("image read failed task_id=%s", task.task_id)
                    external_context += "\n[图片读取失败，不能假装看过图片。]"
            if code_question:
                code_context = await self.repository_reader.scan(message.text)

            try:
                reply = await self.responder.generate(
                    message,
                    decision,
                    context,
                    external_context=external_context,
                    code_context=code_context,
                    image_paths=image_paths,
                )
            except ResponderError:
                reply = self.responder.fallback(decision)

            if not self.store.is_current(task.task_id, task.generation):
                return
            if not send_allowed:
                self.store.set_status(task.task_id, "monitor_generated")
                return
            try:
                await self.sender.send(task, reply)
            except SelfReplyDetected:
                if task.conversation_type == "private":
                    self.store.reset_reply_count(task.sender_id)
                self.store.set_status(task.task_id, "cancelled:self_reply")
                return
            except SendBlocked:
                self.store.set_status(task.task_id, "cancelled:send_gate")
                return
            except DwsError:
                self.store.set_status(task.task_id, "failed:send")
                return

            if task.conversation_type == "private":
                self.store.increment_reply_count(task.sender_id)
            self.store.set_status(task.task_id, "sent")
        finally:
            if image_paths:
                self.resources.cleanup_images(image_paths)

    async def _task_loop(self) -> None:
        while not self.stop_event.is_set():
            tasks = self.store.due_tasks(limit=20)
            if not tasks:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                continue
            for task in tasks:
                if self.stop_event.is_set():
                    break
                try:
                    await self._process_task(task)
                except Exception:
                    LOGGER.exception("unexpected task failure task_id=%s", task.task_id)
                    self.store.set_status(task.task_id, "failed:internal")

    async def _reload_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.reload_event.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    if self.config_manager.reload_if_changed():
                        self.reload_event.set()
                except Exception:
                    LOGGER.exception("config file changed but validation failed")
            if not self.reload_event.is_set():
                continue
            self.reload_event.clear()
            try:
                await self._apply_config(self.config_manager.get())
            except Exception:
                LOGGER.exception("config hot reload failed")

    async def _apply_config(self, new_config: Dict[str, Any]) -> None:
        restart_listener = self._listener_signature(new_config) != self._listener_signature(self.config)
        self.config = new_config
        self.dws.update_config(new_config)
        self.repository_reader.update_config(new_config)
        self.resources.update_config(new_config)
        self.responder.update_config(new_config)
        self.sender.update_config(new_config)
        self.resetter.config = new_config
        if restart_listener:
            LOGGER.info("listener-affecting config changed; reconnecting DWS consumers")
            await self.listener.stop()
            self.listener = DwsListenerManager(new_config, self.on_message)
            await self.listener.start()
        LOGGER.info("configuration hot reloaded")

    async def _backfill_recent_private_messages(
        self,
        now: Optional[float] = None,
        start: Optional[float] = None,
    ) -> int:
        current = time.time() if now is None else now
        window = parse_duration(self.config["dws"].get("reconnect_backfill_window", "5m"))
        if window <= 0:
            return 0
        requested_start = current - window if start is None else start
        scan_start = max(self._started_at, current - window, requested_start)
        items = await self.dws.search_recent_private_messages(
            scan_start,
            current,
            max_messages=int(self.config["context"].get("max_messages", 500)),
        )
        messages: List[Message] = []
        for item in items:
            message_id = self.dws.history_item_message_id(item)
            conversation_id = str(item.get("openConversationId") or "")
            sender_id = self.dws.history_item_sender(item)
            received_at = self.dws.history_item_timestamp(item)
            if not message_id or not conversation_id or not sender_id or received_at <= 0:
                continue
            event = {
                "event_id": "reconnect-backfill:%s" % message_id,
                "message_id": message_id,
                "conversation_id": conversation_id,
                "sender_open_dingtalk_id": sender_id,
                "sender": self.dws.history_item_name(item),
                "content": self.dws.history_item_text(item),
                "event_time": int(received_at * 1000),
            }
            messages.append(normalize_event(event, "private"))
        messages.sort(key=lambda message: (message.received_at, message.message_id))
        for message in messages:
            await self.on_message(message)
        LOGGER.info(
            "DWS reconnect backfill scanned=%s window_seconds=%s",
            len(messages),
            int(window),
        )
        return len(messages)

    async def _private_recovery_loop(self) -> None:
        last_scan_at = time.time()
        while not self.stop_event.is_set():
            scan_interval = 60.0
            try:
                scan_interval = parse_duration(
                    self.config["dws"].get("private_recovery_scan_interval", "1m")
                )
                current = time.time()
                if scan_interval > 0 and current - last_scan_at >= scan_interval:
                    scan_until = current
                    await self._backfill_recent_private_messages(
                        now=scan_until,
                        start=last_scan_at - 10,
                    )
                    last_scan_at = scan_until
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("DWS private message recovery failed")
            wait_seconds = max(1.0, scan_interval if scan_interval > 0 else 60.0)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

    async def _context_expiry_loop(self) -> None:
        while not self.stop_event.is_set():
            group_ttl = parse_duration(self.config["context"]["group_memory_ttl"])
            private_ttl = parse_duration(self.config["context"]["max_age"])
            self.context.expire(group_ttl, private_ttl)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
