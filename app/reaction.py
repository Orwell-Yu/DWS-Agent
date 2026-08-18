from __future__ import annotations

import re
from typing import Iterable, Pattern, Tuple

REACTION_RULES: Tuple[Tuple[Pattern[str], str], ...] = (
    (re.compile(r"生日|birthday", re.I), "生日快乐"),
    (re.compile(r"恭喜|祝贺|拿下|通过了|成功了|上线了|发布了", re.I), "撒花"),
    (re.compile(r"谢谢|感谢|辛苦了|辛苦啦|多谢|thank(?:s| you)?", re.I), "感谢"),
    (re.compile(r"厉害|真棒|太棒|优秀|牛(?:逼|啊|呀)?|666|yyds|赞一个", re.I), "赞"),
    (re.compile(r"哈哈|笑死|绷不住|hhh+|lol\b", re.I), "笑哭"),
    (re.compile(r"难过|伤心|委屈|想哭|崩溃|好累|累死|撑不住", re.I), "抱抱"),
    (re.compile(r"灵感|想法|脑洞|方案|一起想|讨论一下|brainstorm", re.I), "灵感"),
    (re.compile(r"为什么|怎么|如何|哪(?:个|些|里)|什么|谁|是否|能不能|可不可以|[?？]", re.I), "思考"),
    (re.compile(r"请|帮我|麻烦|能否|记得|处理一下|看一下|看下|看看|查一下|查下|回复一下", re.I), "收到"),
    (re.compile(r"你好|您好|早上好|下午好|晚上好|晚安|在吗|hello\b|hi\b", re.I), "打招呼"),
    (re.compile(r"二次元|动漫|动画|漫画|轻音|魔法少女小圆|acgn", re.I), "可爱"),
    (re.compile(r"完成|搞定|已解决|修好了|done\b|fixed\b", re.I), "Done"),
)

TEXT_EMOTION_RULES: Tuple[Tuple[Pattern[str], str], ...] = (
    (re.compile(r"律|田井中|笨蛋|baka", re.I), "律，笨蛋！"),
    (re.compile(r"恐怖故事|鬼故事|灵异|闹鬼", re.I), "不要说恐怖故事！"),
    (re.compile(r"好可怕|害怕|吓人|恐怖|惊悚", re.I), "好可怕……"),
    (re.compile(r"乱来|冒险|危险|删库|线上直接|强制|force\b", re.I), "不可以乱来"),
    (re.compile(r"没办法|无奈|又来|算了|服了", re.I), "真拿你没办法"),
    (re.compile(r"认真的|真的假的|离谱|不会吧|确定吗", re.I), "你是认真的吗？"),
    (re.compile(r"马虎|仔细|重要|生产|线上|安全|合同|金额|权限", re.I), "这个可不能马虎"),
    (re.compile(r"为什么|怎么|如何|分析|想想|研究|[?？]", re.I), "等一下，让我想想"),
    (re.compile(r"不确定|需要确认|再核实", re.I), "需要进一步确认"),
    (re.compile(r"羁绊|友情|朋友|伙伴|并肩|一起", re.I), "羁绊！"),
    (re.compile(r"请|帮我|麻烦|看一下|看下|看看|查一下|查下|收到", re.I), "收到，我认真看看"),
)


def select_group_reaction(text: str, fallback: str = "收到") -> str:
    value = text.strip()
    for pattern, emoji in REACTION_RULES:
        if pattern.search(value):
            return emoji
    return fallback


def select_text_emotion(
    text: str,
    fallback: str = "收到，我认真看看",
    sender_id: str = "",
    sender_name: str = "",
    string_sender_ids: Iterable[str] = (),
    string_sender_names: Iterable[str] = (),
    targeted_emotion: str = "特别回应",
) -> str:
    normalized_sender_id = sender_id.strip()
    if normalized_sender_id and normalized_sender_id in {
        str(value).strip() for value in string_sender_ids
    }:
        return targeted_emotion
    normalized_sender = sender_name.strip()
    if normalized_sender and normalized_sender in {
        str(value).strip() for value in string_sender_names
    }:
        return targeted_emotion
    value = text.strip()
    for pattern, emotion in TEXT_EMOTION_RULES:
        if pattern.search(value):
            return emotion
    return fallback
