from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .config import ConfigError, ConfigManager

LOGGER = logging.getLogger(__name__)
CONFIRM_SEND = "确认开启真实发送"
CONFIRM_RESUME = "确认恢复回复"


class AdminServer:
    def __init__(
        self,
        config_manager: ConfigManager,
        status_provider: Callable[[], Dict[str, Any]],
        reload_callback: Callable[[], None],
        restart_callback: Optional[Callable[[], None]] = None,
    ):
        self.config_manager = config_manager
        self.status_provider = status_provider
        self.reload_callback = reload_callback
        self.restart_callback = restart_callback
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.index_html = Path(__file__).with_name("admin_ui.html").read_text(encoding="utf-8")

    def preferences(self) -> Dict[str, Any]:
        config = self.config_manager.get()
        return {
            "safety": {
                "send_enabled": bool(config["safety"].get("send_enabled", False)),
                "send_scope": config["safety"].get("send_scope", "disabled"),
                "paused": bool(config["safety"].get("paused", False)),
            },
            "private_chat": {
                key: config["private_chat"].get(key)
                for key in (
                    "mode",
                    "delay",
                    "max_auto_replies",
                    "whitelist_max_auto_replies",
                    "whitelist",
                    "blacklist",
                )
            },
            "groups": {
                key: config["groups"].get(key)
                for key in ("mode", "delay", "whitelist", "blacklist", "ignore_at_all")
            },
            "group_reaction": {
                key: config["group_reaction"].get(key)
                for key in (
                    "enabled",
                    "mode",
                    "fallback_emoji",
                    "fallback_text_emotion",
                    "targeted_text_emotion",
                    "string_sender_ids",
                    "string_sender_names",
                    "text_emotions",
                    "timeout_seconds",
                )
            },
            "identity": {
                key: config["identity"].get(key, "")
                for key in (
                    "name",
                    "owner_name",
                    "self_introduction",
                    "private_ai_suffix",
                    "group_ai_suffix",
                )
            },
            "prompts": {
                key: config["prompts"].get(key, "")
                for key in (
                    "personality",
                    "custom_system",
                    "ethics_boundary",
                    "ethics_refusal",
                )
            },
            "repository": {
                key: config["repository"].get(key)
                for key in ("path", "allowed_paths", "remote", "allow_fetch")
            },
            "codex": {
                key: config["codex"].get(key)
                for key in ("model", "reasoning_effort")
            },
        }

    def read_logs(self, lines: int) -> str:
        lines = max(1, min(lines, 500))
        path = Path(self.config_manager.get()["logging"]["file"]).expanduser().resolve()
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return "".join(deque(handle, maxlen=lines))
        except FileNotFoundError:
            return "日志文件尚不存在。\n"

    def _handler(self) -> type:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DwsAutoReplyAdmin/0.2"

            def log_message(self, fmt: str, *args: Any) -> None:
                LOGGER.info("admin %s", fmt % args)

            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                parsed = urlparse(origin)
                return parsed.hostname in {"127.0.0.1", "localhost", "::1"}

            def _write(self, status: int, body: Any, content_type: str = "application/json") -> None:
                if isinstance(body, (dict, list)):
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                elif isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = bytes(body)
                self.send_response(status)
                self.send_header("Content-Type", "%s; charset=utf-8" % content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'",
                )
                self.end_headers()
                self.wfile.write(data)

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 262144:
                    raise ConfigError("invalid request size")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ConfigError("JSON body must be an object")
                return value

            def _authorized_write(self) -> bool:
                return self._origin_ok() and self.headers.get("X-DWS-Auto-Reply") == "1"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._write(HTTPStatus.OK, parent.index_html, "text/html")
                elif parsed.path == "/api/config":
                    self._write(HTTPStatus.OK, {"yaml": parent.config_manager.yaml_text()})
                elif parsed.path == "/api/preferences":
                    self._write(HTTPStatus.OK, parent.preferences())
                elif parsed.path == "/api/status":
                    self._write(HTTPStatus.OK, parent.status_provider())
                elif parsed.path == "/api/tasks":
                    status = parent.status_provider()
                    self._write(HTTPStatus.OK, {"tasks": status.get("recent_tasks", [])})
                elif parsed.path == "/api/logs":
                    query = parse_qs(parsed.query)
                    try:
                        lines = int(query.get("lines", ["100"])[0])
                    except ValueError:
                        lines = 100
                    self._write(HTTPStatus.OK, {"text": parent.read_logs(lines)})
                else:
                    self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_PUT(self) -> None:
                if not self._authorized_write():
                    self._write(HTTPStatus.FORBIDDEN, {"error": "write request rejected"})
                    return
                try:
                    payload = self._payload()
                    allow_sending = payload.get("confirmation") == CONFIRM_SEND
                    if self.path == "/api/config":
                        text = payload.get("yaml")
                        if not isinstance(text, str):
                            raise ConfigError("yaml must be a string")
                        parent.config_manager.update_from_yaml(text, allow_sending)
                    elif self.path == "/api/preferences":
                        preferences = payload.get("preferences")
                        parent.config_manager.update_preferences(preferences, allow_sending)
                    else:
                        self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    parent.reload_callback()
                    self._write(HTTPStatus.OK, {"success": True})
                except (ConfigError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._write(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

            def do_POST(self) -> None:
                if not self._authorized_write():
                    self._write(HTTPStatus.FORBIDDEN, {"error": "write request rejected"})
                    return
                try:
                    payload = self._payload() if self.headers.get("Content-Length") else {}
                    if self.path == "/api/control/pause":
                        parent.config_manager.set_paused(True)
                        parent.reload_callback()
                    elif self.path == "/api/control/resume":
                        if payload.get("confirmation") != CONFIRM_RESUME:
                            raise ConfigError("恢复回复需要输入确认词：%s" % CONFIRM_RESUME)
                        parent.config_manager.set_paused(False)
                        parent.reload_callback()
                    elif self.path == "/api/sending/disable":
                        parent.config_manager.disable_sending()
                        parent.reload_callback()
                    elif self.path == "/api/control/restart":
                        if parent.restart_callback is None:
                            raise ConfigError("restart is unavailable")
                        self._write(HTTPStatus.ACCEPTED, {"success": True})
                        threading.Timer(0.3, parent.restart_callback).start()
                        return
                    else:
                        self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    self._write(HTTPStatus.OK, {"success": True})
                except (ConfigError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._write(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        return Handler

    def start(self, host: str, port: int) -> None:
        if self.server:
            return
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="admin-http", daemon=True
        )
        self.thread.start()
        LOGGER.info("admin UI listening on http://%s:%s", host, port)

    async def stop(self) -> None:
        if not self.server:
            return
        server = self.server
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
        self.server = None
        self.thread = None
