from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Tuple

from .config import group_ids, special_user_ids
from .models import Decision, Message

SENSITIVE_RULES = {
    "money": (
        r"\b(?:money|payment|salary|invoice|reimburse(?:ment)?)\b",
        r"钱(?!包)|金额|付款|支付|转账|打款|收款|报销|发票|薪资|工资|奖金|红包|借款|欠款",
    ),
    "contract": (r"\bcontract\b", r"合同|协议|签约|法务|违约|条款|盖章"),
    "performance": (r"\bperformance review\b", r"绩效|考核|晋升|调薪|末位|评价"),
    "leave": (r"\b(?:leave request|time off|vacation)\b", r"请假|休假|病假|事假|调休|年假"),
    "customer_commitment": (
        r"\b(?:customer|client).*(?:promise|commit|guarantee)\b",
        r"客户.*(?:承诺|保证|兜底|答应)|(?:承诺|保证|兜底).*(?:客户|交付)",
    ),
    "privacy": (
        r"\b(?:privacy|private data|passport|identity card)\b",
        r"隐私|身份证|护照|家庭住址|手机号|银行卡|个人信息|通讯录|家庭成员",
    ),
    "account_security": (
        r"\b(?:password|credential|api key|access token|account security|2fa|mfa)\b",
        r"密码|验证码|账号安全|账户安全|密钥|令牌|登录态|二次验证|双因素|盗号|"
        r"邮箱.*(?:key|token|密钥|密码)",
    ),
}

CODE_RE = re.compile(
    r"代码|编程|报错|异常|堆栈|接口|API|函数|类|模块|仓库|git|python|java|go\b|"
    r"typescript|javascript|sql|数据库|表结构|字段|索引|迁移|alembic|redis|mysql|postgres|"
    r"mongodb|clickhouse|kafka|k8s|kubectl|pod\b|服务调用|源码|"
    r"agent\s*loop|实现细节|内部实现|怎么实现|"
    r"(?:这|那|哪|某|一)张表|(?:数据|关联|订单|映射|关系)表|表里|表中|表名|"
    r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b|\b[a-z]+(?:[A-Z][a-z0-9]*)+\b",
    re.I,
)

SELF_INTRO_RE = re.compile(
    r"你是谁|您是谁|自我介绍|介绍一下(?:你自己)?|你叫什么|你的身份|who are you|introduce yourself",
    re.I,
)

ETHICS_JOKE_RE = re.compile(
    r"(?:爸爸|妈妈|爷爷|奶奶|外公|外婆|哥哥|姐姐|弟弟|妹妹|儿子|女儿|老公|老婆)"
    r"(?:的(?:爸爸|妈妈|爷爷|奶奶|外公|外婆|哥哥|姐姐|弟弟|妹妹|儿子|女儿|老公|老婆))+"
    r"[^。！？\n]{0,20}(?:叫(?:什么|啥)|是(?:谁|什么)|什么关系|怎么称呼|算什么)",
    re.I,
)


def sensitive_category(text: str) -> Tuple[bool, str]:
    for category, patterns in SENSITIVE_RULES.items():
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            return True, category
    return False, ""


def is_special_care(message: Message, config: Dict[str, Any]) -> bool:
    return message.sender_id in special_user_ids(config)


def _matches_any(value: str, candidates: Iterable[Any]) -> bool:
    return value in {str(item) for item in candidates}


class DecisionEngine:
    def decide(
        self,
        message: Message,
        config: Dict[str, Any],
        reply_count: int,
        already_replied: bool = False,
    ) -> Decision:
        special = is_special_care(message, config)

        if message.conversation_type == "private":
            private = config["private_chat"]
            mode = private.get("mode", "all")
            if mode == "disabled":
                return Decision("ignore", "private_chat_disabled")
            if _matches_any(message.sender_id, private.get("blacklist", [])):
                return Decision("ignore", "private_blacklist")
            if mode == "whitelist" and not _matches_any(
                message.sender_id, private.get("whitelist", [])
            ):
                return Decision("ignore", "private_not_whitelisted")
        else:
            groups = config["groups"]
            mode = groups.get("mode", "all")
            if mode == "disabled":
                return Decision("ignore", "groups_disabled")
            if _matches_any(message.conversation_id, groups.get("blacklist", [])):
                return Decision("ignore", "group_blacklist")
            if mode == "whitelist" and message.conversation_id not in group_ids(config):
                return Decision("ignore", "group_not_whitelisted")
            if groups.get("ignore_at_all", True) and (
                "<@all>" in message.raw_content or "@所有人" in message.raw_content
            ):
                return Decision("ignore", "at_all_ignored")

        if already_replied:
            return Decision("ignore", "already_replied")

        if message.conversation_type == "private" and not special:
            private = config["private_chat"]
            if _matches_any(message.sender_id, private.get("whitelist", [])):
                maximum = private.get("whitelist_max_auto_replies")
            else:
                maximum = private.get("max_auto_replies")
            if maximum is not None and reply_count >= maximum:
                return Decision("ignore", "private_reply_limit")

        ignored = set(config["content"].get("ignored_types", []))
        supported = set(config["content"].get("supported_types", []))
        if message.content_type in ignored or message.content_type not in supported:
            return Decision("ignore", "unsupported_%s" % message.content_type)

        if ETHICS_JOKE_RE.search(message.text):
            return Decision(
                "fixed",
                "ethics_joke_refused",
                category="ethics_joke",
                fixed_reply=str(config["prompts"]["ethics_refusal"]).strip(),
            )

        sensitive, category = sensitive_category(message.text)
        if sensitive:
            return Decision(
                "fixed",
                "sensitive_%s" % category,
                category="sensitive",
                fixed_reply="这类内容涉及敏感事项，我是 AI，不能代你处理或作出决定，请联系本人。",
            )

        trigger_keywords = [
            str(value).lower()
            for value in config["repository"].get("trigger_keywords", [])
        ]
        repository_triggered = any(
            keyword in message.text.lower() for keyword in trigger_keywords
        )
        if CODE_RE.search(message.text) or repository_triggered:
            allowed = config["repository"].get("full_detail_requester_ids", [])
            if _matches_any(message.sender_id, allowed):
                return Decision("generate", "code_or_database", category="code")
            return Decision(
                "generate",
                "code_summary_only",
                category="code_summary",
            )
        if SELF_INTRO_RE.search(message.text):
            return Decision("generate", "self_introduction", category="self_intro")
        anime_keywords = [str(value) for value in config["prompts"].get("anime_keywords", [])]
        if any(keyword.lower() in message.text.lower() for keyword in anime_keywords):
            return Decision("generate", "anime", category="anime")
        return Decision("generate", "general", category="general")
