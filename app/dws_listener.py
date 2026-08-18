from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from .models import Message, Task, normalize_event
from .paths import PROJECT_ROOT

LOGGER = logging.getLogger(__name__)


class DwsError(RuntimeError):
    pass


class DwsRunner:
    def __init__(self, config: Dict[str, Any]):
        self.update_config(config)

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.binary = config["dws"]["binary"]
        self.profile = config["dws"]["profile"]
        self.config_dir = config["dws"]["config_dir"]
        self.timezone = ZoneInfo(config["timezone"])

    def environment(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["DWS_CONFIG_DIR"] = self.config_dir
        return env

    async def run_json(
        self,
        args: Iterable[str],
        timeout: float = 30,
        cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        command = [self.binary] + list(args) + [
            "--profile",
            self.profile,
            "--format",
            "json",
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=self.environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.send_signal(signal.SIGTERM)
            await process.wait()
            raise DwsError("DWS command timed out") from exc
        if process.returncode != 0:
            error_text = stderr.decode("utf-8", "replace").strip()
            if not error_text:
                error_text = stdout.decode("utf-8", "replace").strip()
            raise DwsError("DWS command failed (%s): %s" % (process.returncode, error_text[:2000]))
        try:
            return json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DwsError("DWS returned non-JSON output") from exc

    @staticmethod
    def _find_message_dicts(value: Any) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                result.extend(DwsRunner._find_message_dicts(item))
        elif isinstance(value, dict):
            lowered = {str(key).lower() for key in value}
            message_markers = {
                "messageid",
                "message_id",
                "openmessageid",
                "msgid",
                "createtime",
                "create_time",
                "senderopendingtalkid",
                "sender_open_dingtalk_id",
            }
            if lowered & message_markers and (
                lowered & {"content", "text", "messageid", "message_id", "openmessageid"}
            ):
                result.append(value)
            else:
                for child in value.values():
                    result.extend(DwsRunner._find_message_dicts(child))
        return result

    @staticmethod
    def _pick(item: Dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in item and item[name] is not None:
                return item[name]
        lowered = {str(key).lower(): value for key, value in item.items()}
        for name in names:
            if name.lower() in lowered and lowered[name.lower()] is not None:
                return lowered[name.lower()]
        return None

    def _history_time(self, timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=self.timezone).strftime("%Y-%m-%d %H:%M:%S")

    async def fetch_history(
        self,
        conversation_type: str,
        conversation_id: str,
        peer_id: str,
        since: float,
        limit: int,
        direction: str = "newer",
    ) -> List[Dict[str, Any]]:
        args = [
            "chat",
            "message",
            "list",
            "--time",
            self._history_time(since),
            "--direction",
            direction,
            "--limit",
            str(limit),
        ]
        if conversation_type == "group":
            args.extend(["--group", conversation_id])
        else:
            args.extend(["--open-dingtalk-id", peer_id])
        payload = await self.run_json(args, timeout=45)
        return self._find_message_dicts(payload)

    def history_item_sender(self, item: Dict[str, Any]) -> str:
        value = self._pick(
            item,
            "sender_open_dingtalk_id",
            "senderOpenDingTalkId",
            "senderId",
            "sender_id",
            "userId",
            "user_id",
        )
        if isinstance(value, dict):
            value = self._pick(value, "openDingTalkId", "userId", "id")
        return str(value or "")

    def history_item_message_id(self, item: Dict[str, Any]) -> str:
        return str(
            self._pick(item, "message_id", "messageId", "openMessageId", "msgId") or ""
        )

    def history_item_text(self, item: Dict[str, Any]) -> str:
        value = self._pick(item, "content", "text", "message")
        if isinstance(value, dict):
            value = self._pick(value, "text", "content", "title")
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value or "")

    def history_item_name(self, item: Dict[str, Any]) -> str:
        value = self._pick(item, "sender", "senderName", "sender_name", "name", "nick")
        if isinstance(value, dict):
            value = self._pick(value, "name", "nick", "displayName")
        return str(value or "")

    def history_item_timestamp(self, item: Dict[str, Any]) -> float:
        value = self._pick(item, "create_time", "createTime", "timestamp", "sendTime")
        if isinstance(value, (int, float)):
            return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        if isinstance(value, str):
            if value.isdigit():
                numeric = float(value)
                return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    parsed = datetime.strptime(value, fmt)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=self.timezone)
                    return parsed.timestamp()
                except ValueError:
                    pass
        return 0.0

    @staticmethod
    def private_search_messages(payload: Any) -> List[Dict[str, Any]]:
        """Extract only one-to-one messages while preserving the conversation ID."""
        found: List[Dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for child in value:
                    walk(child)
                return
            if not isinstance(value, dict):
                return
            messages = value.get("messages")
            if value.get("singleChat") is True and isinstance(messages, list):
                conversation_id = str(value.get("openConversationId") or "")
                for item in messages:
                    if not isinstance(item, dict):
                        continue
                    normalized = dict(item)
                    normalized.setdefault("openConversationId", conversation_id)
                    found.append(normalized)
                return
            for child in value.values():
                walk(child)

        walk(payload)
        return found

    async def search_recent_private_messages(
        self,
        start: float,
        end: float,
        max_messages: int = 500,
    ) -> List[Dict[str, Any]]:
        start_text = datetime.fromtimestamp(start, tz=self.timezone).isoformat(timespec="seconds")
        end_text = datetime.fromtimestamp(end, tz=self.timezone).isoformat(timespec="seconds")
        cursor = "0"
        found: List[Dict[str, Any]] = []
        seen_ids = set()
        while len(found) < max_messages:
            payload = await self.run_json(
                [
                    "chat",
                    "message",
                    "search-advanced",
                    "--start",
                    start_text,
                    "--end",
                    end_text,
                    "--limit",
                    "30",
                    "--cursor",
                    cursor,
                ],
                timeout=45,
            )
            for item in self.private_search_messages(payload):
                message_id = self.history_item_message_id(item)
                if not message_id or message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                found.append(item)
                if len(found) >= max_messages:
                    break
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            if not isinstance(result, dict) or result.get("hasMore") is not True:
                break
            next_cursor = str(result.get("nextCursor") or "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return found

    async def has_self_reply(self, task: Task) -> bool:
        return bool(await self.self_messages_since(task))

    async def self_messages_since(self, task: Task) -> List[Dict[str, Any]]:
        limit = int(self.config.get("context", {}).get("max_messages", 500))
        items = await self.fetch_history(
            task.conversation_type,
            task.conversation_id,
            task.sender_id,
            time.time(),
            limit,
            direction="older",
        )
        own_ids = {
            str(self.config["identity"].get("user_id", "")),
            str(self.config["identity"].get("open_dingtalk_id", "")),
        }
        return [
            item
            for item in items
            if (
            self.history_item_sender(item) in own_ids
            and self.history_item_timestamp(item) >= task.received_at
            )
        ]

    @staticmethod
    def extract_message_id(value: Any) -> str:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in {
                    "messageid",
                    "message_id",
                    "openmessageid",
                    "msgid",
                } and isinstance(child, str):
                    return child
                found = DwsRunner.extract_message_id(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = DwsRunner.extract_message_id(child)
                if found:
                    return found
        return ""

    async def recover_message(self, task: Task) -> Optional[Message]:
        items = await self.fetch_history(
            task.conversation_type,
            task.conversation_id,
            task.sender_id,
            max(0, task.received_at - 60),
            100,
        )
        for item in items:
            if self.history_item_message_id(item) == task.last_user_message_id:
                event = {
                    "event_id": "history:%s" % task.last_user_message_id,
                    "message_id": task.last_user_message_id,
                    "conversation_id": task.conversation_id,
                    "sender_open_dingtalk_id": task.sender_id,
                    "content": self.history_item_text(item),
                    "event_time": int(task.received_at * 1000),
                }
                return normalize_event(event, "private" if task.conversation_type == "private" else "at")
        return None


@dataclass(frozen=True)
class ListenerSpec:
    name: str
    event_key: str
    source: str
    group: Optional[str] = None


class DwsListenerManager:
    def __init__(
        self,
        config: Dict[str, Any],
        on_message: Callable[[Message], Awaitable[None]],
    ):
        self.config = config
        self.on_message = on_message
        self._stopping = asyncio.Event()
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._tasks: List[asyncio.Task] = []
        self._ready: Dict[str, bool] = {}
        self._retry_after: Dict[str, float] = {}
        self._boot_time = time.time()

    def _specs(self) -> List[ListenerSpec]:
        event_keys = self.config["dws"]["event_keys"]
        specs = [
            ListenerSpec("private-all", event_keys["private"], "private"),
            ListenerSpec("at-me", event_keys["at"], "at"),
        ]
        for index, group in enumerate(self.config["groups"]["whitelist"]):
            specs.append(
                ListenerSpec(
                    "group-%d" % (index + 1),
                    event_keys["group"],
                    "group_context",
                    group=group["conversation_id"],
                )
            )
        return specs

    def status(self) -> Dict[str, Any]:
        return {
            "ready": dict(self._ready),
            "running": {
                name: process.returncode is None for name, process in self._processes.items()
            },
            "next_retry_at": {
                name: value for name, value in self._retry_after.items() if value > time.time()
            },
        }

    async def start(self) -> None:
        if self._tasks:
            return
        for spec in self._specs():
            self._ready[spec.name] = False
            self._tasks.append(asyncio.create_task(self._supervise(spec), name=spec.name))

    async def _supervise(self, spec: ListenerSpec) -> None:
        backoff = 1.0
        maximum = float(self.config["dws"].get("reconnect_max_seconds", 60))
        while not self._stopping.is_set():
            requested_delay = 0.0
            try:
                await self._consume_once(spec)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("DWS listener %s failed", spec.name)
                requested_delay = max(0.0, self._retry_after.get(spec.name, 0.0) - time.time())
            if self._stopping.is_set():
                break
            self._ready[spec.name] = False
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=max(backoff, requested_delay)
                )
            except asyncio.TimeoutError:
                pass
            backoff = min(maximum, backoff * 2)

    async def _consume_once(self, spec: ListenerSpec) -> None:
        dws = self.config["dws"]
        command = [
            dws["binary"],
            "event",
            "consume",
            spec.event_key,
            "--flatten",
            "--format",
            "ndjson",
            "--ttl",
            "24h",
            "--ephemeral",
            "--name",
            "dws-auto-reply-%s" % spec.name,
            "--profile",
            dws["profile"],
        ]
        if spec.group:
            command.extend(["--group", spec.group])
        env = os.environ.copy()
        env["DWS_CONFIG_DIR"] = dws["config_dir"]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[spec.name] = process
        stderr_task = asyncio.create_task(self._read_stderr(spec, process))
        LOGGER.info("started DWS listener %s pid=%s", spec.name, process.pid)
        try:
            assert process.stdout is not None
            while not self._stopping.is_set():
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    LOGGER.warning("ignored malformed NDJSON from listener %s", spec.name)
                    continue
                message = normalize_event(event, spec.source)
                if message.received_at + 2 < self._boot_time:
                    LOGGER.info(
                        "ignored pre-start event listener=%s message_id=%s",
                        spec.name,
                        message.message_id,
                    )
                    continue
                await self.on_message(message)
            return_code = await process.wait()
            try:
                await asyncio.wait_for(stderr_task, timeout=2)
            except asyncio.TimeoutError:
                stderr_task.cancel()
            if not self._stopping.is_set() and return_code != 0:
                raise DwsError("listener %s exited with %s" % (spec.name, return_code))
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            self._processes.pop(spec.name, None)
            self._ready[spec.name] = False

    async def _read_stderr(
        self, spec: ListenerSpec, process: asyncio.subprocess.Process
    ) -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            retry_match = re.search(r'"retry_after_seconds"\s*:\s*(\d+)', text)
            if retry_match:
                delay = int(retry_match.group(1))
                self._retry_after[spec.name] = time.time() + delay
                LOGGER.warning("DWS listener %s honoring retry_after=%ss", spec.name, delay)
            if "[event] ready" in text:
                self._ready[spec.name] = True
                self._retry_after.pop(spec.name, None)
                LOGGER.info("DWS listener ready: %s", spec.name)
            elif text:
                LOGGER.info("DWS listener %s: %s", spec.name, text[:1000])

    async def stop(self) -> None:
        self._stopping.set()
        processes = list(self._processes.items())
        for name, process in processes:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                LOGGER.info("closed stdin for DWS listener %s", name)
        for name, process in processes:
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                LOGGER.warning("DWS listener %s did not stop on stdin close; sending SIGTERM", name)
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    LOGGER.error("DWS listener %s still running; refusing SIGKILL", name)
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
