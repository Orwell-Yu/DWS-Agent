from copy import deepcopy

import yaml


def test_config():
    with open("config.example.yaml", "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    data["safety"].update(
        {
            "send_enabled": True,
            "send_scope": "all",
            "allowed_private_ids": ["special-user"],
        }
    )
    data["private_chat"]["whitelist"] = ["special-user"]
    data["groups"]["whitelist"] = [
        {
            "conversation_id": "group-immediate",
            "name": "立即回复测试群",
            "immediate_reply": True,
        },
        {
            "conversation_id": "group-delayed",
            "name": "延迟回复测试群",
            "immediate_reply": False,
        },
    ]
    data["special_care"]["users"] = [
        {
            "name": "特别关心测试用户",
            "ids": ["special-user"],
            "immediate": True,
            "max_auto_replies": None,
            "group_requires_at": True,
        }
    ]
    data["identity"].update(
        {
            "name": "测试助手",
            "owner_name": "测试所有者",
            "self_introduction": "你好，我是测试助手，是协助测试所有者处理消息的 AI。",
            "user_id": "self-user-id",
            "open_dingtalk_id": "self-open-id",
        }
    )
    data["prompts"].update(
        {
            "personality": "认真、自然、简洁地回复。",
            "anime": "熟悉动画内容；不确定时明确说明。",
            "anime_keywords": ["轻音少女", "魔法少女小圆"],
        }
    )
    data["codex"].update(
        {"binary": "codex", "model": "test-model", "reasoning_effort": "low"}
    )
    data["dws"].update(
        {
            "binary": "dws",
            "config_dir": "/tmp/dws-auto-reply-test-dws",
            "profile": "test-profile",
        }
    )
    data["repository"].update(
        {
            "path": "/tmp/dws-auto-reply-test-repository",
            "full_detail_requester_ids": ["special-user"],
            "trigger_keywords": ["sample-repo", "example-platform", "示例办公"],
        }
    )
    data["logging"]["file"] = "/tmp/dws-auto-reply-test.log"
    return deepcopy(data)
