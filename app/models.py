from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.I)
MENTION_RE = re.compile(r"<@[^>]+>", re.I)
LEADING_NAME_MENTION_RE = re.compile(r"^\s*\@[^\s，,：:]{1,80}\s*", re.I)


@dataclass
class Message:
    event_id: str
    message_id: str
    conversation_id: str
    sender_id: str
    sender_name: str
    conversation_type: str
    received_at: float
    content_type: str
    text: str
    raw_content: str = ""
    urls: List[str] = field(default_factory=list)
    resource_ids: List[str] = field(default_factory=list)
    source: str = ""


@dataclass
class Task:
    task_id: str
    message_id: str
    conversation_id: str
    sender_id: str
    conversation_type: str
    received_at: float
    reply_due_at: float
    reply_count: int
    last_user_message_id: str
    last_self_message_at: Optional[float]
    status: str
    generation: int
    special_care: bool = False


@dataclass
class Decision:
    action: str
    reason: str
    category: str = "general"
    fixed_reply: Optional[str] = None


def _timestamp(event: Dict[str, Any]) -> float:
    for key in ("event_time", "timestamp"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        if isinstance(value, str) and value.isdigit():
            numeric = float(value)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    value = event.get("create_time")
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(value, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                pass
    return datetime.now(tz=timezone.utc).timestamp()


def _walk_values(value: Any, key_names: set) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in key_names and isinstance(child, (str, int)):
                found.append(str(child))
            found.extend(_walk_values(child, key_names))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_values(child, key_names))
    return found


def _content_payload(raw: Any) -> tuple:
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return json.dumps(raw, ensure_ascii=False), raw
    stripped = raw.strip()
    if stripped.startswith(("{", "[")):
        try:
            return raw, json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return raw, None


def _text_from_payload(raw: str, payload: Any) -> str:
    if payload is None:
        return raw.strip()
    candidates = _walk_values(payload, {"text", "content", "title", "url", "link"})
    clean = [item.strip() for item in candidates if item and item.strip()]
    return "\n".join(dict.fromkeys(clean))[:20000]


def _detect_type(event: Dict[str, Any], payload: Any, text: str) -> str:
    type_candidates = []
    for key in ("msg_type", "msgType", "message_type", "messageType", "content_type"):
        if event.get(key):
            type_candidates.append(str(event[key]).lower())
    if isinstance(payload, dict):
        type_candidates.extend(
            str(value).lower()
            for key, value in payload.items()
            if key.lower() in {"msgtype", "messagetype", "type"} and value is not None
        )
    combined = " ".join(type_candidates)
    if any(token in combined for token in ("audio", "voice")):
        return "audio"
    if "video" in combined:
        return "video"
    if any(token in combined for token in ("sticker", "dynamicemotion", "expression")):
        return "sticker"
    if any(token in combined for token in ("image", "picture", "photo")):
        return "image"
    if any(token in combined for token in ("emotion", "emoji")):
        return "dingtalk_emotion"
    if "file" in combined:
        return "file"
    urls = URL_RE.findall(text)
    if any("alidocs.dingtalk.com" in url.lower() for url in urls):
        return "dingtalk_doc"
    if urls:
        return "link"
    if text and len(text) <= 16 and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]{3,}", text):
        return "emoji"
    return "text"


def normalize_event(event: Dict[str, Any], source: str) -> Message:
    raw_content, payload = _content_payload(event.get("content"))
    text = _text_from_payload(raw_content, payload)
    if source == "at":
        text = MENTION_RE.sub("", text).strip()
        text = LEADING_NAME_MENTION_RE.sub("", text).strip()
    resource_ids = _walk_values(payload, {"mediaid", "media_id", "fileid", "file_id"}) if payload else []
    urls = URL_RE.findall(text)
    message_id = str(event.get("message_id") or event.get("event_id") or "")
    event_id = str(event.get("event_id") or message_id)
    conversation_id = str(event.get("conversation_id") or "")
    sender_id = str(
        event.get("sender_open_dingtalk_id")
        or event.get("senderOpenDingTalkId")
        or event.get("sender_id")
        or ""
    )
    sender_name = str(event.get("sender") or event.get("sender_name") or "")
    return Message(
        event_id=event_id,
        message_id=message_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        sender_name=sender_name,
        conversation_type="private" if source == "private" else "group",
        received_at=_timestamp(event),
        content_type=_detect_type(event, payload, text),
        text=text,
        raw_content=raw_content,
        urls=urls,
        resource_ids=list(dict.fromkeys(resource_ids)),
        source=source,
    )
