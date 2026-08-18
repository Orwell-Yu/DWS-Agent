from __future__ import annotations

import copy
import os
import re
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List

import yaml


class ConfigError(ValueError):
    pass


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.I)


def parse_duration(value: Any) -> float:
    if isinstance(value, (int, float)):
        if value < 0:
            raise ConfigError("duration must not be negative")
        return float(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ConfigError("invalid duration: %r" % (value,))
    number = float(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    return number * multiplier


def _require(mapping: Dict[str, Any], path: Iterable[str]) -> Any:
    cursor: Any = mapping
    traversed = []
    for part in path:
        traversed.append(part)
        if not isinstance(cursor, dict) or part not in cursor:
            raise ConfigError("missing config key: %s" % ".".join(traversed))
        cursor = cursor[part]
    return cursor


def validate_config(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ConfigError("top-level YAML value must be a mapping")

    for key in (
        "timezone",
        "safety",
        "private_chat",
        "groups",
        "group_reaction",
        "special_care",
        "identity",
        "prompts",
        "context",
        "codex",
        "dws",
        "repository",
        "retry",
        "reset",
        "web",
    ):
        _require(data, (key,))

    parse_duration(_require(data, ("private_chat", "delay")))
    parse_duration(_require(data, ("groups", "delay")))
    parse_duration(_require(data, ("context", "max_age")))
    parse_duration(_require(data, ("context", "group_memory_ttl")))
    parse_duration(_require(data, ("reset", "seen_message_retention")))
    parse_duration(_require(data, ("reset", "send_log_retention")))
    parse_duration(_require(data, ("reset", "restart_task_max_lateness")))

    max_replies = _require(data, ("private_chat", "max_auto_replies"))
    if max_replies is not None and (not isinstance(max_replies, int) or max_replies < 1):
        raise ConfigError("private_chat.max_auto_replies must be null or a positive integer")
    whitelist_max_replies = _require(data, ("private_chat", "whitelist_max_auto_replies"))
    if whitelist_max_replies is not None and (
        not isinstance(whitelist_max_replies, int) or whitelist_max_replies < 1
    ):
        raise ConfigError(
            "private_chat.whitelist_max_auto_replies must be null or a positive integer"
        )

    private = data["private_chat"]
    if private.get("mode") not in {"all", "whitelist", "disabled"}:
        raise ConfigError("private_chat.mode must be all, whitelist, or disabled")
    for key in ("whitelist", "blacklist"):
        values = private.get(key)
        if not isinstance(values, list) or any(not str(value).strip() for value in values):
            raise ConfigError("private_chat.%s must be a list of IDs" % key)

    groups = _require(data, ("groups", "whitelist"))
    if not isinstance(groups, list):
        raise ConfigError("groups.whitelist must be a list")
    group_config = data["groups"]
    if group_config.get("mode") not in {"all", "whitelist", "disabled"}:
        raise ConfigError("groups.mode must be all, whitelist, or disabled")
    blacklist = group_config.get("blacklist")
    if not isinstance(blacklist, list) or any(not str(value).strip() for value in blacklist):
        raise ConfigError("groups.blacklist must be a list of conversation IDs")
    seen_groups = set()
    for group in groups:
        if not isinstance(group, dict) or not group.get("conversation_id") or not group.get("name"):
            raise ConfigError("every group requires conversation_id and name")
        if not isinstance(group.get("immediate_reply", False), bool):
            raise ConfigError("groups.whitelist[].immediate_reply must be true or false")
        if group["conversation_id"] in seen_groups:
            raise ConfigError("duplicate group conversation_id")
        seen_groups.add(group["conversation_id"])

    reaction = data["group_reaction"]
    if not isinstance(reaction.get("enabled"), bool):
        raise ConfigError("group_reaction.enabled must be true or false")
    if reaction.get("strategy") != "auto":
        raise ConfigError("group_reaction.strategy must be auto")
    mode = reaction.get("mode", "emoji")
    if mode not in {"emoji", "text_emotion"}:
        raise ConfigError("group_reaction.mode must be emoji or text_emotion")
    fallback = reaction.get("fallback_emoji")
    if not isinstance(fallback, str) or not fallback.strip():
        raise ConfigError("group_reaction.fallback_emoji must be a non-empty DWS emoji name")
    text_emotions = reaction.get("text_emotions", [])
    if not isinstance(text_emotions, list):
        raise ConfigError("group_reaction.text_emotions must be a list")
    emotion_names = set()
    for item in text_emotions:
        if not isinstance(item, dict):
            raise ConfigError("group_reaction.text_emotions rows must be objects")
        for key in ("name", "text", "emotion_id", "background_id"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ConfigError(
                    "group_reaction.text_emotions[].%s must be a non-empty string" % key
                )
        if item["name"] in emotion_names:
            raise ConfigError("duplicate group_reaction text emotion name")
        emotion_names.add(item["name"])
    fallback_text = reaction.get("fallback_text_emotion")
    targeted_text = reaction.get("targeted_text_emotion", "特别回应")
    string_sender_ids = reaction.get("string_sender_ids", [])
    if not isinstance(string_sender_ids, list) or any(
        not isinstance(value, str) or not value.strip() for value in string_sender_ids
    ):
        raise ConfigError("group_reaction.string_sender_ids must be a list of IDs")
    string_sender_names = reaction.get("string_sender_names", [])
    if not isinstance(string_sender_names, list) or any(
        not isinstance(value, str) or not value.strip() for value in string_sender_names
    ):
        raise ConfigError("group_reaction.string_sender_names must be a list of names")
    if mode == "text_emotion":
        if not isinstance(fallback_text, str) or fallback_text not in emotion_names:
            raise ConfigError(
                "group_reaction.fallback_text_emotion must name a configured text emotion"
            )
        if (string_sender_ids or string_sender_names) and (
            not isinstance(targeted_text, str) or targeted_text not in emotion_names
        ):
            raise ConfigError(
                "group_reaction.targeted_text_emotion must name a configured text emotion"
            )
    timeout = reaction.get("timeout_seconds", 10)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
        raise ConfigError("group_reaction.timeout_seconds must be in 1..30")

    users = _require(data, ("special_care", "users"))
    if not isinstance(users, list):
        raise ConfigError("special_care.users must be a list")
    for user in users:
        if not isinstance(user, dict) or not user.get("name") or not user.get("ids"):
            raise ConfigError("every special-care user requires name and ids")

    safety = data["safety"]
    if safety.get("reply_only") is not True:
        raise ConfigError("safety.reply_only must remain true")
    if safety.get("send_scope") not in {"disabled", "allowlist", "all"}:
        raise ConfigError("safety.send_scope must be disabled, allowlist, or all")
    if safety.get("send_enabled") and safety.get("send_scope") == "disabled":
        raise ConfigError("send_enabled cannot be true while send_scope is disabled")
    if not isinstance(safety.get("paused"), bool):
        raise ConfigError("safety.paused must be true or false")

    web = data["web"]
    if web.get("host") != "127.0.0.1":
        raise ConfigError("web.host must remain 127.0.0.1")
    port = web.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("web.port must be in 1..65535")

    identity = data["identity"]
    for key in ("name", "owner_name", "self_introduction"):
        if not isinstance(identity.get(key), str) or not identity[key].strip():
            raise ConfigError("identity.%s must be a non-empty string" % key)

    prompts = data["prompts"]
    for key in ("personality", "custom_system", "ethics_boundary"):
        if not isinstance(prompts.get(key), str) or len(prompts[key]) > 20000:
            raise ConfigError("prompts.%s must be a string of at most 20000 characters" % key)
    ethics_refusal = prompts.get("ethics_refusal")
    if not isinstance(ethics_refusal, str) or not ethics_refusal.strip() or len(ethics_refusal) > 500:
        raise ConfigError("prompts.ethics_refusal must be a non-empty string of at most 500 characters")
    anime_keywords = prompts.get("anime_keywords", [])
    if not isinstance(anime_keywords, list) or any(not str(value).strip() for value in anime_keywords):
        raise ConfigError("prompts.anime_keywords must be a list of strings")

    repository = data["repository"]
    repository_path = repository.get("path")
    if not isinstance(repository_path, str) or not repository_path.strip():
        raise ConfigError("repository.path must be a non-empty local path")
    if repository.get("read_only") is not True or repository.get("database_access") is not False:
        raise ConfigError("repository must stay read-only with database access disabled")
    full_detail_requesters = repository.get("full_detail_requester_ids")
    if not isinstance(full_detail_requesters, list):
        raise ConfigError("repository.full_detail_requester_ids must be a list")
    if any(not isinstance(value, str) or not value.strip() for value in full_detail_requesters):
        raise ConfigError("repository.full_detail_requester_ids must contain non-empty strings")
    if (
        repository.get("read_local_git_refs") is not True
        or repository.get("allow_checkout") is not False
        or not isinstance(repository.get("allow_fetch"), bool)
    ):
        raise ConfigError("repository Git access settings are unsafe")
    allowed_paths = repository.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise ConfigError("repository.allowed_paths must be a non-empty list")
    for value in allowed_paths:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("repository.allowed_paths must contain non-empty strings")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ConfigError("repository.allowed_paths must stay inside the repository")
    trigger_keywords = repository.get("trigger_keywords", [])
    if not isinstance(trigger_keywords, list) or any(
        not isinstance(value, str) or not value.strip() for value in trigger_keywords
    ):
        raise ConfigError("repository.trigger_keywords must be a list of strings")
    remote = repository.get("remote", "origin")
    if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", remote):
        raise ConfigError("repository.remote is invalid")
    scan_timeout = repository.get("scan_timeout_seconds", 20)
    if not isinstance(scan_timeout, (int, float)) or not 1 <= scan_timeout <= 60:
        raise ConfigError("repository.scan_timeout_seconds must be in 1..60")
    fetch_timeout = repository.get("fetch_timeout_seconds", 20)
    if not isinstance(fetch_timeout, (int, float)) or not 1 <= fetch_timeout <= 60:
        raise ConfigError("repository.fetch_timeout_seconds must be in 1..60")
    batch_size = repository.get("branch_scan_batch_size", 32)
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 128:
        raise ConfigError("repository.branch_scan_batch_size must be in 1..128")

    codex = data["codex"]
    model = codex.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise ConfigError("codex.model must be a non-empty string of at most 128 characters")
    if codex.get("reasoning_effort") not in {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }:
        raise ConfigError(
            "codex.reasoning_effort must be low, medium, high, xhigh, max, or ultra"
        )

    dws = data["dws"]
    backfill_window = parse_duration(dws.get("reconnect_backfill_window", "5m"))
    if backfill_window != 0 and not 60 <= backfill_window <= 3600:
        raise ConfigError("dws.reconnect_backfill_window must be 0s or between 1m and 1h")
    recovery_scan = parse_duration(dws.get("private_recovery_scan_interval", "1m"))
    if recovery_scan != 0 and not 30 <= recovery_scan <= 600:
        raise ConfigError(
            "dws.private_recovery_scan_interval must be 0s or between 30s and 10m"
        )
    event_keys = dws.get("event_keys")
    if not isinstance(event_keys, dict):
        raise ConfigError("dws.event_keys must be a mapping")
    for name in ("private", "at", "group"):
        value = event_keys.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ConfigError("dws.event_keys.%s must be a non-empty string" % name)


class ConfigManager:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._mtime_ns = 0
        self.reload()

    def reload(self) -> Dict[str, Any]:
        text = self.path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        validate_config(loaded)
        with self._lock:
            self._data = loaded
            self._mtime_ns = self.path.stat().st_mtime_ns
            return copy.deepcopy(self._data)

    def reload_if_changed(self) -> bool:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return False
        with self._lock:
            unchanged = mtime_ns == self._mtime_ns
        if unchanged:
            return False
        self.reload()
        return True

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def yaml_text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @staticmethod
    def _validate_send_change(
        current: Dict[str, Any], loaded: Dict[str, Any], allow_enable_sending: bool
    ) -> None:
        if not allow_enable_sending:
            was_enabled = bool(current["safety"].get("send_enabled"))
            wants_enabled = bool(loaded["safety"].get("send_enabled"))
            if wants_enabled and not was_enabled:
                raise ConfigError("the localhost UI cannot enable real sending")
            old_scope = current["safety"].get("send_scope", "disabled")
            new_scope = loaded["safety"].get("send_scope", "disabled")
            rank = {"disabled": 0, "allowlist": 1, "all": 2}
            if rank[new_scope] > rank[old_scope]:
                raise ConfigError("the localhost UI cannot broaden the real-send scope")

    def _write_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        validate_config(data)
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        fd, temp_name = tempfile.mkstemp(prefix="config-", suffix=".yaml", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                if not text.endswith("\n"):
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return self.reload()

    def update_from_yaml(self, text: str, allow_enable_sending: bool = False) -> Dict[str, Any]:
        loaded = yaml.safe_load(text)
        validate_config(loaded)
        self._validate_send_change(self.get(), loaded, allow_enable_sending)
        return self._write_config(loaded)

    @staticmethod
    def _id_list(value: Any, field: str) -> List[str]:
        if not isinstance(value, list):
            raise ConfigError("%s must be a list" % field)
        result = []
        for item in value:
            text = str(item).strip()
            if not text or len(text) > 512:
                raise ConfigError("%s contains an invalid ID" % field)
            if text not in result:
                result.append(text)
        return result

    def update_preferences(
        self, payload: Dict[str, Any], allow_enable_sending: bool = False
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ConfigError("preferences must be an object")
        data = self.get()
        safety_input = payload.get("safety", {})
        private_input = payload.get("private_chat", {})
        groups_input = payload.get("groups", {})
        reaction_input = payload.get("group_reaction", {})
        identity_input = payload.get("identity", {})
        prompts_input = payload.get("prompts", {})
        repository_input = payload.get("repository", {})
        codex_input = payload.get("codex", {})
        for name, value in (
            ("safety", safety_input),
            ("private_chat", private_input),
            ("groups", groups_input),
            ("group_reaction", reaction_input),
            ("identity", identity_input),
            ("prompts", prompts_input),
            ("repository", repository_input),
            ("codex", codex_input),
        ):
            if not isinstance(value, dict):
                raise ConfigError("%s preferences must be an object" % name)

        if "send_enabled" in safety_input:
            data["safety"]["send_enabled"] = bool(safety_input["send_enabled"])
        if "send_scope" in safety_input:
            data["safety"]["send_scope"] = str(safety_input["send_scope"])

        for key in ("mode", "delay"):
            if key in private_input:
                data["private_chat"][key] = private_input[key]
        for key in ("max_auto_replies", "whitelist_max_auto_replies"):
            if key in private_input:
                maximum = private_input[key]
                data["private_chat"][key] = (
                    None if maximum in (None, "") else int(maximum)
                )
        for key in ("whitelist", "blacklist"):
            if key in private_input:
                data["private_chat"][key] = self._id_list(
                    private_input[key], "private_chat.%s" % key
                )

        for key in ("mode", "delay", "ignore_at_all"):
            if key in groups_input:
                data["groups"][key] = groups_input[key]
        if "blacklist" in groups_input:
            data["groups"]["blacklist"] = self._id_list(
                groups_input["blacklist"], "groups.blacklist"
            )
        if "whitelist" in groups_input:
            rows = groups_input["whitelist"]
            if not isinstance(rows, list):
                raise ConfigError("groups.whitelist must be a list")
            existing = {
                str(item["conversation_id"]): item
                for item in data["groups"].get("whitelist", [])
            }
            normalized = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ConfigError("groups.whitelist rows must be objects")
                conversation_id = str(row.get("conversation_id", "")).strip()
                name = str(row.get("name", "") or conversation_id).strip()
                if not conversation_id or len(conversation_id) > 512 or len(name) > 200:
                    raise ConfigError("groups.whitelist contains an invalid row")
                previous = existing.get(conversation_id, {})
                normalized.append(
                    {
                        "conversation_id": conversation_id,
                        "name": name,
                        "immediate_reply": bool(previous.get("immediate_reply", False)),
                    }
                )
            data["groups"]["whitelist"] = normalized

        if "enabled" in reaction_input:
            data["group_reaction"]["enabled"] = bool(reaction_input["enabled"])
        for key in (
            "mode",
            "fallback_emoji",
            "fallback_text_emotion",
            "targeted_text_emotion",
        ):
            if key in reaction_input:
                data["group_reaction"][key] = str(reaction_input[key]).strip()
        for key in ("string_sender_ids", "string_sender_names"):
            if key in reaction_input:
                data["group_reaction"][key] = self._id_list(
                    reaction_input[key],
                    "group_reaction.%s" % key,
                )
        if "timeout_seconds" in reaction_input:
            data["group_reaction"]["timeout_seconds"] = int(
                reaction_input["timeout_seconds"]
            )
        if "text_emotions" in reaction_input:
            rows = reaction_input["text_emotions"]
            if not isinstance(rows, list):
                raise ConfigError("group_reaction.text_emotions must be a list")
            normalized = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ConfigError("group_reaction.text_emotions rows must be objects")
                normalized.append(
                    {
                        key: str(row.get(key, "")).strip()
                        for key in ("name", "text", "emotion_id", "background_id")
                    }
                )
            data["group_reaction"]["text_emotions"] = normalized

        for key in (
            "name",
            "owner_name",
            "self_introduction",
            "private_ai_suffix",
            "group_ai_suffix",
        ):
            if key in identity_input:
                data["identity"][key] = str(identity_input[key])
        for key in ("personality", "custom_system", "ethics_boundary", "ethics_refusal"):
            if key in prompts_input:
                data["prompts"][key] = str(prompts_input[key])

        if "path" in repository_input:
            data["repository"]["path"] = str(repository_input["path"])
        if "allowed_paths" in repository_input:
            data["repository"]["allowed_paths"] = self._id_list(
                repository_input["allowed_paths"], "repository.allowed_paths"
            )
        if "remote" in repository_input:
            data["repository"]["remote"] = str(repository_input["remote"])
        if "allow_fetch" in repository_input:
            data["repository"]["allow_fetch"] = bool(repository_input["allow_fetch"])

        if "model" in codex_input:
            data["codex"]["model"] = str(codex_input["model"]).strip()
        if "reasoning_effort" in codex_input:
            data["codex"]["reasoning_effort"] = str(
                codex_input["reasoning_effort"]
            ).strip()

        validate_config(data)
        self._validate_send_change(self.get(), data, allow_enable_sending)
        return self._write_config(data)

    def set_paused(self, paused: bool) -> Dict[str, Any]:
        data = self.get()
        data["safety"]["paused"] = bool(paused)
        return self._write_config(data)

    def disable_sending(self) -> Dict[str, Any]:
        data = self.get()
        data["safety"]["send_enabled"] = False
        data["safety"]["send_scope"] = "disabled"
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        return self.update_from_yaml(text, allow_enable_sending=True)


def group_ids(config: Dict[str, Any]) -> set:
    return {item["conversation_id"] for item in config["groups"]["whitelist"]}


def special_user_ids(config: Dict[str, Any]) -> set:
    result = set()
    for item in config["special_care"]["users"]:
        result.update(str(value) for value in item.get("ids", []))
    return result
