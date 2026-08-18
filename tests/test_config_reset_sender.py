import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from support import test_config

from app.config import ConfigError, ConfigManager, parse_duration
from app.message_store import MessageStore
from app.models import Message, Task
from app.reset_state import ResetScheduler
from app.sender import MessageSender, SendBlocked


def config_data():
    return test_config()


def disabled_config_data():
    data = config_data()
    data["safety"]["send_enabled"] = False
    data["safety"]["send_scope"] = "disabled"
    data["safety"]["allowed_private_ids"] = []
    data["safety"]["allowed_group_ids"] = []
    return data


def make_task(generation=0):
    return Task(
        task_id="private:conv",
        message_id="m1",
        conversation_id="conv",
        sender_id="user-open-id",
        conversation_type="private",
        received_at=1000,
        reply_due_at=1000,
        reply_count=0,
        last_user_message_id="m1",
        last_self_message_at=None,
        status="generating",
        generation=generation,
    )


class ConfigTests(unittest.TestCase):
    def test_open_source_example_config_is_valid(self):
        manager = ConfigManager("config.example.yaml")
        self.assertEqual([], manager.get()["repository"]["full_detail_requester_ids"])

    def test_duration_parser(self):
        self.assertEqual(600, parse_duration("10m"))
        self.assertEqual(86400, parse_duration("24h"))
        with self.assertRaises(ConfigError):
            parse_duration("tomorrow")

    def test_ui_cannot_enable_or_broaden_send_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            data = disabled_config_data()
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            manager = ConfigManager(str(path))
            data["safety"]["send_enabled"] = True
            data["safety"]["send_scope"] = "all"
            with self.assertRaises(ConfigError):
                manager.update_from_yaml(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def test_unsafe_repository_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            data = config_data()
            data["repository"]["database_access"] = True
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ConfigError):
                ConfigManager(str(path))

    def test_repository_checkout_and_escaping_paths_are_rejected(self):
        for key, value in (
            ("allow_checkout", True),
            ("allowed_paths", ["../outside"]),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                data = config_data()
                data["repository"][key] = value
                path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    ConfigManager(str(path))


class ResetTests(unittest.TestCase):
    def test_boundary_is_4am_shanghai(self):
        cfg = config_data()
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(str(Path(directory) / "state.sqlite3"))
            scheduler = ResetScheduler(cfg, store, lambda: None)
            zone = ZoneInfo("Asia/Shanghai")
            before = datetime(2026, 8, 9, 3, 59, tzinfo=zone)
            after = datetime(2026, 8, 9, 4, 1, tzinfo=zone)
            self.assertEqual(datetime(2026, 8, 8, 4, 0, tzinfo=zone), scheduler.current_boundary(before))
            self.assertEqual(datetime(2026, 8, 9, 4, 0, tzinfo=zone), scheduler.current_boundary(after))
            store.close()


class DummyDws:
    def __init__(self):
        self.calls = []

    async def run_json(self, args, timeout=30):
        self.calls.append(args)
        return {"success": True}


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MessageStore(str(Path(self.temp.name) / "state.sqlite3"))
        message = Message(
            event_id="e1",
            message_id="m1",
            conversation_id="conv",
            sender_id="user-open-id",
            sender_name="name",
            conversation_type="private",
            received_at=1000,
            content_type="text",
            text="hello",
            source="private",
        )
        self.store.record_seen(message)
        self.store.schedule(message, 1000, special_care=False)
        task = self.store.due_tasks(now=1000)[0]
        self.store.claim(task.task_id, task.generation)
        self.task = make_task(task.generation)
        self.dws = DummyDws()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_real_send_gate_blocks_by_default(self):
        sender = MessageSender(
            disabled_config_data(), self.dws, self.store, lambda task: asyncio.sleep(0, False)
        )
        with self.assertRaises(SendBlocked):
            await sender.send(self.task, "hello")

    async def test_paused_gate_blocks_even_when_real_sending_is_enabled(self):
        data = config_data()
        data["safety"]["paused"] = True
        sender = MessageSender(
            data, self.dws, self.store, lambda task: asyncio.sleep(0, False)
        )
        with self.assertRaises(SendBlocked):
            await sender.send(self.task, "hello")
        self.assertEqual([], self.dws.calls)

    async def test_allowed_send_adds_suffix_and_uuid(self):
        cfg = config_data()
        cfg["safety"].update(
            {
                "send_enabled": True,
                "send_scope": "allowlist",
                "allowed_private_ids": ["user-open-id"],
            }
        )

        async def no_self_reply(task):
            return False

        sender = MessageSender(cfg, self.dws, self.store, no_self_reply)
        send_uuid = await sender.send(self.task, "你好")
        self.assertEqual("sent", self.store.send_status(send_uuid))
        args = self.dws.calls[0]
        text = args[args.index("--text") + 1]
        self.assertEqual("你好\n\n本回复由AI生成", text)
        self.assertEqual(["chat", "message", "reply"], args[:3])
        self.assertEqual("conv", args[args.index("--conversation-id") + 1])
        self.assertEqual("m1", args[args.index("--ref-msg-id") + 1])
        self.assertEqual("user-open-id", args[args.index("--ref-sender") + 1])
        self.assertIn("--uuid", args)
        self.assertIn("--ai-tag=true", args)

    async def test_forged_or_proactive_task_is_blocked(self):
        cfg = config_data()
        cfg["safety"].update({"send_enabled": True, "send_scope": "all"})

        async def no_self_reply(task):
            return False

        sender = MessageSender(cfg, self.dws, self.store, no_self_reply)
        forged = Task(**{**self.task.__dict__, "message_id": "not-an-inbound-message"})
        with self.assertRaises(SendBlocked):
            await sender.send(forged, "不能主动发送")
        self.assertEqual([], self.dws.calls)


if __name__ == "__main__":
    unittest.main()
