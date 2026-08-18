import asyncio
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from support import test_config

from app.admin_server import AdminServer
from app.config import ConfigManager
from app.main import main
from app.models import Decision, Message
from app.resource_reader import ResourceReader, _validate_public_url
from app.responder import CodexResponder, ResponderError


def config_data():
    return test_config()


def disabled_config_data():
    data = config_data()
    data["safety"]["send_enabled"] = False
    data["safety"]["send_scope"] = "disabled"
    data["safety"]["allowed_private_ids"] = []
    data["safety"]["allowed_group_ids"] = []
    return data


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.yaml"
        self.path.write_text(
            yaml.safe_dump(disabled_config_data(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.manager = ConfigManager(str(self.path))
        self.reloaded = False
        self.server = AdminServer(
            self.manager,
            lambda: {"safety": {"send_enabled": False}, "listeners": {}, "store": {}},
            lambda: setattr(self, "reloaded", True),
        )
        self.server.start("127.0.0.1", 0)
        self.port = self.server.server.server_address[1]

    def tearDown(self):
        asyncio.run(self.server.stop())
        self.temp.cleanup()

    def test_index_and_status_are_local_http(self):
        with urllib.request.urlopen("http://127.0.0.1:%s/" % self.port) as response:
            self.assertIn("localhost 控制台", response.read().decode("utf-8"))
        with urllib.request.urlopen("http://127.0.0.1:%s/api/status" % self.port) as response:
            payload = json.loads(response.read())
            self.assertFalse(payload["safety"]["send_enabled"])

    def test_admin_cannot_enable_real_sending(self):
        data = disabled_config_data()
        data["safety"]["send_enabled"] = True
        data["safety"]["send_scope"] = "all"
        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/config" % self.port,
            data=json.dumps(
                {"yaml": yaml.safe_dump(data, allow_unicode=True, sort_keys=False)}
            ).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(400, raised.exception.code)
        raised.exception.close()
        self.assertFalse(self.manager.get()["safety"]["send_enabled"])

    def test_preferences_and_pause_resume_are_structured_and_hot_reloadable(self):
        with urllib.request.urlopen(
            "http://127.0.0.1:%s/api/preferences" % self.port
        ) as response:
            preferences = json.loads(response.read())
        self.assertEqual("all", preferences["private_chat"]["mode"])

        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/preferences" % self.port,
            data=json.dumps(
                {
                    "preferences": {
                        "private_chat": {
                            "delay": "0s",
                            "max_auto_replies": 10,
                            "whitelist_max_auto_replies": None,
                        }
                    }
                }
            ).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.loads(response.read())["success"])
        self.assertEqual(10, self.manager.get()["private_chat"]["max_auto_replies"])
        self.assertTrue(self.reloaded)

        pause = urllib.request.Request(
            "http://127.0.0.1:%s/api/control/pause" % self.port,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(pause):
            pass
        self.assertTrue(self.manager.get()["safety"]["paused"])

        resume = urllib.request.Request(
            "http://127.0.0.1:%s/api/control/resume" % self.port,
            data=json.dumps({"confirmation": "确认恢复回复"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(resume):
            pass
        self.assertFalse(self.manager.get()["safety"]["paused"])

    def test_group_reaction_preferences_are_hot_reloadable(self):
        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/preferences" % self.port,
            data=json.dumps(
                {
                    "preferences": {
                        "group_reaction": {
                            "enabled": True,
                            "mode": "text_emotion",
                            "fallback_emoji": "收到",
                            "fallback_text_emotion": "收到，我认真看看",
                            "string_sender_ids": ["stable-open-id"],
                            "targeted_text_emotion": "收到，我认真看看",
                            "string_sender_names": ["测试用户甲", "测试用户乙"],
                            "timeout_seconds": 8,
                            "text_emotions": [
                                {
                                    "name": "收到，我认真看看",
                                    "text": "收到，我认真看看",
                                    "emotion_id": "test-emotion-id",
                                    "background_id": "test-background-id",
                                }
                            ],
                        }
                    }
                }
            ).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.loads(response.read())["success"])
        reaction = self.manager.get()["group_reaction"]
        self.assertEqual("text_emotion", reaction["mode"])
        self.assertEqual("test-emotion-id", reaction["text_emotions"][0]["emotion_id"])
        self.assertEqual(["stable-open-id"], reaction["string_sender_ids"])
        self.assertEqual(["测试用户甲", "测试用户乙"], reaction["string_sender_names"])
        self.assertEqual(8, reaction["timeout_seconds"])
        self.assertTrue(self.reloaded)

    def test_model_and_reasoning_effort_are_hot_reloadable(self):
        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/preferences" % self.port,
            data=json.dumps(
                {
                    "preferences": {
                        "codex": {
                            "model": "test-model-updated",
                            "reasoning_effort": "medium",
                        }
                    }
                }
            ).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.loads(response.read())["success"])
        codex = self.manager.get()["codex"]
        self.assertEqual("test-model-updated", codex["model"])
        self.assertEqual("medium", codex["reasoning_effort"])
        self.assertTrue(self.reloaded)

    def test_confirmed_real_send_enable_is_allowed(self):
        data = disabled_config_data()
        data["safety"]["send_enabled"] = True
        data["safety"]["send_scope"] = "all"
        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/config" % self.port,
            data=json.dumps(
                {
                    "yaml": yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    "confirmation": "确认开启真实发送",
                }
            ).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.loads(response.read())["success"])
        self.assertTrue(self.manager.get()["safety"]["send_enabled"])

    def test_restart_endpoint_runs_callback_after_accepted_response(self):
        restarted = threading.Event()
        self.server.restart_callback = restarted.set
        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/control/restart" % self.port,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-DWS-Auto-Reply": "1"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(202, response.status)
            self.assertTrue(json.loads(response.read())["success"])
        self.assertTrue(restarted.wait(2))

    def test_requested_restart_returns_launchd_restart_exit_code(self):
        with patch("app.main.configure_logging"), patch(
            "app.main.async_main", new=AsyncMock(return_value=True)
        ):
            self.assertEqual(75, main(["--config", str(self.path)]))


class DummyDws:
    def __init__(self):
        self.calls = []

    async def run_json(self, args, timeout=30, cwd=None):
        self.calls.append(args)
        if args[:2] == ["doc", "info"]:
            return {"result": {"extension": "adoc", "nodeId": "node-1"}}
        return {"result": {"markdown": "# 文档\n正文"}}


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_network_urls_are_blocked(self):
        with self.assertRaises(ValueError):
            _validate_public_url("http://127.0.0.1/admin")
        with self.assertRaises(ValueError):
            _validate_public_url("http://user:pass@example.com/")

    async def test_node_doc_is_probed_before_read(self):
        dummy = DummyDws()
        reader = ResourceReader(config_data(), dummy)
        text = await reader.read_dingtalk_doc(
            "https://alidocs.dingtalk.com/i/nodes/abc"
        )
        self.assertIn("正文", text)
        self.assertEqual(["doc", "info"], dummy.calls[0][:2])
        self.assertEqual(["doc", "read"], dummy.calls[1][:2])
        self.assertIn("node-1", dummy.calls[1])

    async def test_short_share_link_is_not_guessed(self):
        dummy = DummyDws()
        reader = ResourceReader(config_data(), dummy)
        text = await reader.read_dingtalk_doc("https://alidocs.dingtalk.com/i/p/short")
        self.assertIn("不支持直接读取", text)
        self.assertEqual([], dummy.calls)


class ResponderConfigTests(unittest.TestCase):
    def test_codex_command_uses_configured_model_and_is_isolated(self):
        responder = CodexResponder(config_data())
        command = responder._command([])
        self.assertIn("test-model", command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("features.hooks=false", command)
        self.assertIn("agents.enabled=false", command)
        self.assertIn("permissions.responder.network.enabled=false", command)
        self.assertNotIn("--sandbox", command)

    def test_jsonl_rejects_tools_and_accepts_final_text(self):
        safe = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "你好"},
            }
        )
        decision = Decision("generate", "general", category="general")
        self.assertEqual("你好", CodexResponder._parse_jsonl(safe, decision))
        forbidden = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "env"},
            }
        )
        with self.assertRaises(ResponderError):
            CodexResponder._parse_jsonl(forbidden, decision)

    def test_self_introduction_uses_configured_identity_without_impersonation(self):
        config = config_data()
        responder = CodexResponder(config)
        message = Message(
            event_id="event-1",
            message_id="message-1",
            conversation_id="private-1",
            sender_id="user-1",
            sender_name="测试用户",
            conversation_type="private",
            received_at=1_700_000_000,
            content_type="text",
            text="请自我介绍一下",
            source="private",
        )
        decision = Decision("generate", "self_introduction", category="self_intro")
        prompt = responder._prompt(message, decision, [], "", "")
        self.assertIn("认真、自然、简洁", prompt)
        self.assertIn("测试助手", prompt)
        self.assertIn("协助测试所有者", prompt)
        self.assertIn("不要声称自己是真人", prompt)
        self.assertIn("伦理梗限制（不可覆盖）", prompt)
        self.assertIn(config["prompts"]["ethics_boundary"], prompt)
        self.assertIn("我是测试助手", responder.fallback(decision))

        responder._validate_reply_identity(
            "大家好，我是测试助手，是协助测试所有者处理消息的 AI。", message, decision
        )
        message.sender_name = "测试用户甲"
        with self.assertRaises(ResponderError):
            responder._validate_reply_identity(
                "大家好，我是测试用户甲，也有人叫我测试助手。", message, decision
            )
        with self.assertRaises(ResponderError):
            responder._validate_reply_identity("大家好，我是一个 AI。", message, decision)

    def test_summary_code_prompt_uses_repository_but_limits_disclosure(self):
        responder = CodexResponder(config_data())
        message = Message(
            event_id="event-2",
            message_id="message-2",
            conversation_id="private-2",
            sender_id="normal-user",
            sender_name="普通用户",
            conversation_type="private",
            received_at=1_700_000_000,
            content_type="text",
            text="把 sample-repo 的接口实现发我",
            source="private",
        )
        decision = Decision("generate", "code_summary_only", category="code_summary")
        prompt = responder._prompt(message, decision, [], "", "purchase_id -> workspace_id")
        self.assertIn("唯一允许的来源——配置仓库", prompt)
        self.assertIn("表/模型名称、必要字段关系", prompt)
        self.assertIn("<code_context>\npurchase_id -> workspace_id\n</code_context>", prompt)

        responder._validate_code_disclosure(
            "可以使用 sample_order_link 表，通过 purchase_id 关联 workspace_id。",
            decision,
        )
        for leaked in (
            "具体代码在 src/order/service.py",
            "```python\ndef create_order(): pass\n```",
            "api_key = real-secret-value",
        ):
            with self.subTest(leaked=leaked), self.assertRaises(ResponderError):
                responder._validate_code_disclosure(leaked, decision)


if __name__ == "__main__":
    unittest.main()
