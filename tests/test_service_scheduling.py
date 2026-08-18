import tempfile
import time
import unittest
from pathlib import Path

import yaml
from support import test_config

from app.models import Message, Task
from app.service import AutoReplyService


def config_data():
    return test_config()


def incoming(sender="normal-user", source="private", kind="private", conversation="private-1"):
    return Message(
        event_id="event-1",
        message_id="message-1",
        conversation_id=conversation,
        sender_id=sender,
        sender_name="测试",
        conversation_type=kind,
        received_at=1000,
        content_type="text",
        text="你好",
        raw_content="你好",
        source=source,
    )


class ServiceSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "config.yaml"
        data = config_data()
        data["group_reaction"]["enabled"] = False
        self.config_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self.service = AutoReplyService(str(self.config_path))

    async def asyncTearDown(self):
        self.service.store.close()
        self.temp.cleanup()

    async def test_exhausted_counter_still_schedules_self_reply_preflight(self):
        for _ in range(3):
            self.service.store.increment_reply_count("normal-user")
        await self.service.on_message(incoming())
        tasks = self.service.store.recent_tasks()
        self.assertEqual(1, len(tasks))
        self.assertEqual("pending", tasks[0]["status"])

    async def test_group_context_does_not_schedule_but_at_event_does(self):
        group_id = self.service.config["groups"]["whitelist"][0]["conversation_id"]
        context = incoming(source="group_context", kind="group", conversation=group_id)
        await self.service.on_message(context)
        self.assertEqual([], self.service.store.recent_tasks())

        trigger = incoming(source="at", kind="group", conversation=group_id)
        trigger.event_id = "event-2"
        await self.service.on_message(trigger)
        self.assertEqual(1, len(self.service.store.recent_tasks()))

    async def test_special_user_is_due_immediately(self):
        item = incoming(sender="special-user")
        await self.service.on_message(item)
        task = self.service.store.due_tasks(now=1000)[0]
        self.assertTrue(task.special_care)
        self.assertEqual(1000, task.reply_due_at)

    async def test_all_private_messages_use_zero_delay(self):
        await self.service.on_message(incoming(sender="normal-user"))
        task = self.service.store.due_tasks(now=1000)[0]
        self.assertEqual(1000, task.reply_due_at)
        self.assertFalse(task.special_care)

    async def test_configured_group_is_immediate_but_other_group_keeps_delay(self):
        immediate_group = self.service.config["groups"]["whitelist"][0]
        delayed_group = self.service.config["groups"]["whitelist"][1]
        self.assertTrue(immediate_group["immediate_reply"])
        self.assertFalse(delayed_group["immediate_reply"])

        first = incoming(source="at", kind="group", conversation=immediate_group["conversation_id"])
        await self.service.on_message(first)
        second = incoming(source="at", kind="group", conversation=delayed_group["conversation_id"])
        second.event_id = "event-2"
        second.message_id = "message-2"
        await self.service.on_message(second)

        tasks = {item["conversation_id"]: item for item in self.service.store.recent_tasks()}
        self.assertEqual(1000, tasks[immediate_group["conversation_id"]]["reply_due_at"])
        self.assertEqual(1600, tasks[delayed_group["conversation_id"]]["reply_due_at"])
        self.assertEqual(1, tasks[immediate_group["conversation_id"]]["special_care"])
        self.assertEqual(0, tasks[delayed_group["conversation_id"]]["special_care"])

    async def test_group_at_adds_configured_emoji_reaction(self):
        calls = []

        async def run_json(args, timeout=30, cwd=None):
            calls.append((args, timeout))
            return {"success": True}

        self.service.dws.run_json = run_json
        self.service.config["group_reaction"]["enabled"] = True
        self.service.config["group_reaction"]["mode"] = "emoji"
        group_id = self.service.config["groups"]["whitelist"][0]["conversation_id"]
        trigger = incoming(source="at", kind="group", conversation=group_id)
        trigger.text = "请帮我看一下这个"
        await self.service.on_message(trigger)
        self.assertEqual(1, len(calls))
        args, timeout = calls[0]
        self.assertEqual(
            [
                "chat",
                "message",
                "add-emoji",
                "--conversation-id",
                group_id,
                "--msg-id",
                "message-1",
                "--emoji",
                "收到",
            ],
            args,
        )
        self.assertEqual(10, timeout)

    async def test_group_at_adds_configured_text_emotion(self):
        calls = []

        async def run_json(args, timeout=30, cwd=None):
            calls.append((args, timeout))
            return {"success": True}

        self.service.dws.run_json = run_json
        self.service.config["group_reaction"]["enabled"] = True
        self.service.config["group_reaction"]["mode"] = "text_emotion"
        self.service.config["group_reaction"]["fallback_text_emotion"] = (
            "收到，我认真看看"
        )
        self.service.config["group_reaction"]["text_emotions"] = [
            {
                "name": "收到，我认真看看",
                "text": "收到，我认真看看",
                "emotion_id": "test-emotion-id",
                "background_id": "test-background-id",
            }
        ]
        group_id = self.service.config["groups"]["whitelist"][0]["conversation_id"]
        trigger = incoming(source="at", kind="group", conversation=group_id)
        trigger.text = "请帮我认真看一下"
        await self.service.on_message(trigger)
        args, timeout = calls[0]
        self.assertEqual("add-text-emotion", args[2])
        self.assertEqual("test-emotion-id", args[args.index("--emotion-id") + 1])
        self.assertEqual("收到，我认真看看", args[args.index("--text") + 1])
        self.assertEqual("test-background-id", args[args.index("--background-id") + 1])
        self.assertEqual(10, timeout)

    async def test_configured_sender_uses_targeted_text_emotion(self):
        calls = []

        async def run_json(args, timeout=30, cwd=None):
            calls.append(args)
            return {"success": True}

        self.service.dws.run_json = run_json
        self.service.config["group_reaction"].update(
            {
                "enabled": True,
                "mode": "text_emotion",
                "string_sender_ids": ["stable-open-id"],
                "targeted_text_emotion": "特别回应",
                "string_sender_names": ["测试用户甲", "测试用户乙"],
                "text_emotions": [
                    {
                        "name": "特别回应",
                        "text": "特别回应",
                        "emotion_id": "test-targeted-emotion-id",
                        "background_id": "test-background-id",
                    }
                ],
            }
        )
        group_id = self.service.config["groups"]["whitelist"][0]["conversation_id"]
        trigger = incoming(source="at", kind="group", conversation=group_id)
        trigger.sender_id = "stable-open-id"
        trigger.sender_name = "测试用户丙"
        trigger.text = "普通消息"
        await self.service.on_message(trigger)
        args = calls[0]
        self.assertEqual("add-text-emotion", args[2])
        self.assertEqual("test-targeted-emotion-id", args[args.index("--emotion-id") + 1])

    async def test_unknown_group_direct_at_is_scheduled_when_all_groups_enabled(self):
        calls = []

        async def run_json(args, timeout=30, cwd=None):
            calls.append(args)
            return {"success": True}

        self.service.dws.run_json = run_json
        self.service.config["groups"]["mode"] = "all"
        self.service.config["group_reaction"]["enabled"] = True
        trigger = incoming(source="at", kind="group", conversation="new-group")
        trigger.event_id = "unknown-group-event"
        await self.service.on_message(trigger)
        self.assertEqual(1, len(self.service.store.recent_tasks()))
        self.assertEqual("new-group", self.service.store.recent_tasks()[0]["conversation_id"])
        self.assertIn("add-emoji", calls[0])

    async def test_at_all_does_not_add_reaction(self):
        calls = []

        async def run_json(args, timeout=30, cwd=None):
            calls.append(args)
            return {"success": True}

        self.service.dws.run_json = run_json
        self.service.config["group_reaction"]["enabled"] = True
        group_id = self.service.config["groups"]["whitelist"][0]["conversation_id"]
        trigger = incoming(source="at", kind="group", conversation=group_id)
        trigger.raw_content = "<@all> 大家好"
        await self.service.on_message(trigger)
        self.assertEqual([], calls)
        self.assertEqual([], self.service.store.recent_tasks())

    async def test_special_private_ignores_known_auto_reply_but_not_manual_reply(self):
        now = time.time()
        task = Task(
            task_id="private:conv:m2",
            message_id="m2",
            conversation_id="conv",
            sender_id="special-user",
            conversation_type="private",
            received_at=now - 2,
            reply_due_at=now - 2,
            reply_count=0,
            last_user_message_id="m2",
            last_self_message_at=None,
            status="generating",
            generation=0,
            special_care=True,
        )
        previous = Task(**{**task.__dict__, "task_id": "private:conv:m1", "message_id": "m1"})
        self.service.store.upsert_send_log(
            "auto-send", previous, 1, "sent", outbound_message_id="auto-message"
        )

        async def auto_only(_task):
            return [
                {
                    "messageId": "auto-message",
                    "senderOpenDingTalkId": self.service.config["identity"]["open_dingtalk_id"],
                    "createTime": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            ]

        self.service.dws.self_messages_since = auto_only
        self.assertFalse(await self.service._has_self_reply(task))

        async def manual(_task):
            return [
                {
                    "messageId": "manual-message",
                    "senderOpenDingTalkId": self.service.config["identity"]["open_dingtalk_id"],
                    "createTime": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            ]

        self.service.dws.self_messages_since = manual
        self.assertTrue(await self.service._has_self_reply(task))

    async def test_normal_users_code_questions_scan_only_configured_repository(self):
        item = incoming()
        item.text = (
            "sample_order_link 这张表什么时候有记录？"
            "有没有 purchase_id 和 tenant_id 关系表？ https://example.com/other-repo"
        )
        item.urls = ["https://example.com/other-repo"]
        await self.service.on_message(item)
        task = self.service.store.due_tasks(now=1600)[0]
        observed = {}

        async def no_self_reply(_task):
            return False

        async def context(_task, _message):
            return []

        async def scan(question):
            observed["question"] = question
            return "repository evidence"

        async def generate(
            message,
            decision,
            context,
            external_context="",
            code_context="",
            image_paths=None,
        ):
            observed["category"] = decision.category
            observed["external_context"] = external_context
            observed["code_context"] = code_context
            return "可以通过订单关系表关联。"

        async def send(_task, _reply):
            observed["sent"] = True

        async def unexpected_external_read(_urls):
            raise AssertionError("code questions must not read external sources")

        self.service._has_self_reply = no_self_reply
        self.service._build_context = context
        self.service.repository_reader.scan = scan
        self.service.responder.generate = generate
        self.service.sender.send = send
        self.service.resources.read_links = unexpected_external_read
        await self.service._process_task(task)

        self.assertEqual("code_summary", observed["category"])
        self.assertEqual("repository evidence", observed["code_context"])
        self.assertEqual("", observed["external_context"])
        self.assertTrue(observed["sent"])


if __name__ == "__main__":
    unittest.main()
