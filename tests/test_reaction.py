import unittest

from app.reaction import select_group_reaction, select_text_emotion


class ReactionSelectionTests(unittest.TestCase):
    def test_selects_contextual_builtin_reactions(self):
        cases = {
            "请帮我看一下这个": "收到",
            "这个为什么会报错？": "思考",
            "太厉害了，666": "赞",
            "谢谢，辛苦了": "感谢",
            "哈哈哈笑死": "笑哭",
            "今天真的有点难过": "抱抱",
            "来讨论一下新方案": "灵感",
            "你好呀": "打招呼",
            "聊聊轻音少女": "可爱",
            "已经修好了": "Done",
            "祝贺正式上线了": "撒花",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, select_group_reaction(text))

    def test_uses_configured_fallback(self):
        self.assertEqual("收到", select_group_reaction("今天天气不错"))
        self.assertEqual("微笑", select_group_reaction("今天天气不错", fallback="微笑"))

    def test_selects_configured_text_emotions(self):
        cases = {
            "律真是个笨蛋": "律，笨蛋！",
            "讲个恐怖故事吧": "不要说恐怖故事！",
            "这个方案为什么失败？": "等一下，让我想想",
            "帮我认真看一下": "收到，我认真看看",
            "别在线上乱来": "不可以乱来",
            "这也太离谱了": "你是认真的吗？",
            "大家一起努力": "羁绊！",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, select_text_emotion(text))

    def test_text_emotion_uses_configured_fallback(self):
        self.assertEqual("收到，我认真看看", select_text_emotion("今天天气不错"))

    def test_configured_senders_use_targeted_emotion(self):
        senders = ["测试用户甲", "测试用户乙"]
        for sender in senders:
            with self.subTest(sender=sender):
                self.assertEqual(
                    "特别回应",
                    select_text_emotion(
                        "普通消息",
                        sender_name=sender,
                        string_sender_names=senders,
                    ),
                )

    def test_configured_sender_id_takes_precedence_over_event_nickname(self):
        self.assertEqual(
            "特别回应",
            select_text_emotion(
                "普通消息",
                sender_id="stable-open-id",
                sender_name="测试用户丙",
                string_sender_ids=["stable-open-id"],
                string_sender_names=["测试用户甲"],
            ),
        )

    def test_configured_sender_id_works_without_sender_name(self):
        self.assertEqual(
            "特别回应",
            select_text_emotion(
                "普通消息",
                sender_id="stable-open-id",
                string_sender_ids=["stable-open-id"],
            ),
        )


if __name__ == "__main__":
    unittest.main()
