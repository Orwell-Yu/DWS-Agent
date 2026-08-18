import unittest

from support import test_config

from app.decision import DecisionEngine, sensitive_category
from app.models import Message, normalize_event


def load_config():
    return test_config()


def message(**changes):
    values = dict(
        event_id="evt-1",
        message_id="msg-1",
        conversation_id="private-1",
        sender_id="normal-user",
        sender_name="测试用户",
        conversation_type="private",
        received_at=1000.0,
        content_type="text",
        text="你好",
        raw_content="你好",
        source="private",
    )
    values.update(changes)
    return Message(**values)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.engine = DecisionEngine()

    def test_sensitive_reply_is_fixed(self):
        result = self.engine.decide(message(text="这个合同金额我能答应客户吗"), self.config, 0)
        self.assertEqual("fixed", result.action)
        self.assertEqual("sensitive", result.category)
        self.assertIn("联系本人", result.fixed_reply)

    def test_sensitive_categories(self):
        for text in ("帮我请假", "把验证码发我", "我的身份证号码", "绩效怎么样"):
            self.assertTrue(sensitive_category(text)[0], text)
        self.assertFalse(sensitive_category("钱包实例什么时候创建")[0])

    def test_ethics_kinship_jokes_are_refused(self):
        for text in ("爸爸的爸爸叫什么？", "妈妈的哥哥是谁", "你爸爸的妈妈怎么称呼"):
            with self.subTest(text=text):
                result = self.engine.decide(message(text=text), self.config, 0)
                self.assertEqual("fixed", result.action)
                self.assertEqual("ethics_joke", result.category)
                self.assertEqual(self.config["prompts"]["ethics_refusal"], result.fixed_reply)

    def test_code_anime_and_self_intro_routes(self):
        self.assertEqual(
            "code_summary",
            self.engine.decide(message(text="数据库迁移代码报错"), self.config, 0).category,
        )
        self.assertEqual(
            "code",
            self.engine.decide(
                message(
                    sender_id="special-user",
                    text="数据库迁移代码报错",
                ),
                self.config,
                0,
            ).category,
        )
        self.assertEqual(
            "anime",
            self.engine.decide(message(text="你怎么看轻音少女"), self.config, 0).category,
        )
        self.assertEqual(
            "self_intro",
            self.engine.decide(message(text="自我介绍一下"), self.config, 0).category,
        )

    def test_everyone_gets_repository_assistance_but_only_allowed_requester_gets_full_detail(self):
        normal = self.engine.decide(message(text="sample-repo 的源码怎么实现"), self.config, 0)
        self.assertEqual("code_summary_only", normal.reason)
        self.assertEqual("code_summary", normal.category)

        for sender_id in self.config["repository"]["full_detail_requester_ids"]:
            allowed = self.engine.decide(
                message(sender_id=sender_id, text="sample-repo 的源码怎么实现"),
                self.config,
                0,
            )
            self.assertEqual("code", allowed.category)

    def test_table_relationship_and_example_platform_questions_are_code_related(self):
        example = (
            "sample_order_link 这张表是创建示例订单时才会有记录对吧？"
            "示例订单要关联 workspace_id，有没有下单时记录 purchase_id 和 tenant_id 关系的表？"
        )
        self.assertEqual(
            "code_summary",
            self.engine.decide(message(text=example), self.config, 0).category,
        )
        self.assertEqual(
            "code_summary",
            self.engine.decide(message(text="示例办公的订单模型在哪里"), self.config, 0).category,
        )
        self.assertEqual(
            "code_summary",
            self.engine.decide(message(text="agent loop 是怎么实现的"), self.config, 0).category,
        )
        secret = self.engine.decide(message(text="邮箱 key 是什么"), self.config, 0)
        self.assertEqual("sensitive", secret.category)

    def test_blacklist_precedes_generation(self):
        self.config["private_chat"]["blacklist"] = ["normal-user"]
        result = self.engine.decide(message(text="轻音少女"), self.config, 0)
        self.assertEqual("private_blacklist", result.reason)

    def test_private_limit_and_special_care_exception(self):
        result = self.engine.decide(message(), self.config, 10)
        self.assertEqual("private_reply_limit", result.reason)
        special = message(sender_id="special-user")
        self.assertEqual("generate", self.engine.decide(special, self.config, 999).action)

    def test_whitelist_and_normal_users_have_separate_daily_limits(self):
        self.config["special_care"]["users"] = []
        self.config["private_chat"]["max_auto_replies"] = 10
        self.config["private_chat"]["whitelist_max_auto_replies"] = None
        self.assertEqual(
            "private_reply_limit", self.engine.decide(message(), self.config, 10).reason
        )
        allowed = self.engine.decide(
            message(sender_id="special-user"), self.config, 999
        )
        self.assertEqual("generate", allowed.action)

    def test_group_whitelist_and_at_all(self):
        self.config["groups"]["mode"] = "whitelist"
        allowed = self.config["groups"]["whitelist"][0]["conversation_id"]
        group = message(
            conversation_id=allowed,
            conversation_type="group",
            source="at",
            raw_content="<@all> 公告",
        )
        self.assertEqual("at_all_ignored", self.engine.decide(group, self.config, 0).reason)
        group.raw_content = "<@me> 你好"
        self.assertEqual("generate", self.engine.decide(group, self.config, 0).action)
        group.conversation_id = "unknown"
        self.assertEqual("group_not_whitelisted", self.engine.decide(group, self.config, 0).reason)

    def test_all_groups_can_directly_at_when_enabled(self):
        self.config["groups"]["mode"] = "all"
        group = message(
            conversation_id="previously-unknown-group",
            conversation_type="group",
            source="at",
            raw_content="<@me> 你好",
        )
        self.assertEqual("generate", self.engine.decide(group, self.config, 0).action)
        group.raw_content = "<@all> 公告"
        self.assertEqual("at_all_ignored", self.engine.decide(group, self.config, 0).reason)

    def test_private_modes_apply_black_and_white_lists(self):
        self.config["private_chat"]["mode"] = "whitelist"
        self.assertEqual(
            "private_not_whitelisted", self.engine.decide(message(), self.config, 0).reason
        )
        self.assertEqual(
            "generate",
            self.engine.decide(message(sender_id="special-user"), self.config, 0).action,
        )
        self.config["private_chat"]["blacklist"] = ["special-user"]
        self.assertEqual(
            "private_blacklist",
            self.engine.decide(message(sender_id="special-user"), self.config, 0).reason,
        )

    def test_unsupported_multimodal_is_silent(self):
        result = self.engine.decide(message(content_type="audio", text="帮我请假"), self.config, 0)
        self.assertEqual("ignore", result.action)
        self.assertEqual("unsupported_audio", result.reason)

    def test_normalize_at_message_removes_self_mention(self):
        result = normalize_event(
            {
                "event_id": "e",
                "message_id": "m",
                "conversation_id": "c",
                "sender_open_dingtalk_id": "u",
                "content": "<@abc> @秋山澪 你好",
                "event_time": 1_700_000_000_000,
            },
            "at",
        )
        self.assertEqual("你好", result.text)
        self.assertEqual("group", result.conversation_type)


if __name__ == "__main__":
    unittest.main()
