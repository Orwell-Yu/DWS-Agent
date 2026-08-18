import tempfile
import time
import unittest
from pathlib import Path

import yaml
from support import test_config

from app.dws_listener import DwsRunner
from app.service import AutoReplyService


class PrivateSearchParserTests(unittest.TestCase):
    def test_only_single_chat_messages_are_extracted(self):
        payload = {
            "result": {
                "conversationMessagesList": [
                    {
                        "singleChat": True,
                        "openConversationId": "private-conversation",
                        "messages": [{"openMessageId": "private-message"}],
                    },
                    {
                        "singleChat": False,
                        "openConversationId": "group-conversation",
                        "messages": [{"openMessageId": "group-message"}],
                    },
                ]
            }
        }
        messages = DwsRunner.private_search_messages(payload)
        self.assertEqual(1, len(messages))
        self.assertEqual("private-message", messages[0]["openMessageId"])
        self.assertEqual(
            "private-conversation", messages[0]["openConversationId"]
        )


class ReconnectBackfillServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "config.yaml"
        data = test_config()
        data["group_reaction"]["enabled"] = False
        data["dws"]["config_dir"] = str(Path(self.temp.name) / ".dws")
        self.config_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.service = AutoReplyService(str(self.config_path))

    async def asyncTearDown(self):
        self.service.store.close()
        self.temp.cleanup()

    async def test_backfill_schedules_missing_message_and_is_idempotent(self):
        now = time.time()
        create_time = self.service.dws._history_time(now - 30)
        calls = []

        async def run_json(args, timeout=30, cwd=None):
            calls.append(args)
            return {
                "success": True,
                "result": {
                    "hasMore": False,
                    "conversationMessagesList": [
                        {
                            "singleChat": True,
                            "openConversationId": "private-conversation",
                            "messages": [
                                {
                                    "openMessageId": "recovered-message",
                                    "sender": "测试用户",
                                    "senderOpenDingTalkId": "recovered-user",
                                    "createTime": create_time,
                                    "content": "漏掉的消息",
                                }
                            ],
                        }
                    ],
                },
            }

        self.service.dws.run_json = run_json
        recovered = await self.service._backfill_recent_private_messages(now)
        self.assertEqual(1, recovered)
        tasks = self.service.store.recent_tasks()
        self.assertEqual(1, len(tasks))
        self.assertEqual("recovered-message", tasks[0]["message_id"])
        self.assertEqual("recovered-user", tasks[0]["sender_id"])
        self.service.store.set_status(tasks[0]["task_id"], "cancelled:test")

        await self.service._backfill_recent_private_messages(now)
        self.assertEqual("cancelled:test", self.service.store.recent_tasks()[0]["status"])
        self.assertEqual("search-advanced", calls[0][2])
        self.assertEqual("30", calls[0][calls[0].index("--limit") + 1])


if __name__ == "__main__":
    unittest.main()
