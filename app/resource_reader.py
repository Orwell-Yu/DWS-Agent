from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import mimetypes
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .dws_listener import DwsError, DwsRunner
from .models import Message
from .paths import RUNTIME_DIR

LOGGER = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._hidden += 1
        elif tag.lower() in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1
        elif tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(part.split()) for part in self.parts)
        return "\n".join(line for line in lines if line)


def _validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    for _family, _, _, _, address in socket.getaddrinfo(
        parsed.hostname, port, type=socket.SOCK_STREAM
    ):
        ip = ipaddress.ip_address(address[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("private or local network targets are blocked")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ResourceReader:
    def __init__(self, config: Dict[str, Any], dws: DwsRunner):
        self.dws = dws
        self.runtime = RUNTIME_DIR.resolve()
        self.update_config(config)

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        content = config["content"]
        self.max_web_bytes = int(content.get("max_web_bytes", 1024 * 1024))
        self.max_image_bytes = int(content.get("max_image_bytes", 10 * 1024 * 1024))
        self.web_timeout = float(content.get("web_timeout_seconds", 8))

    @staticmethod
    def _extract_doc_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, list):
            return "\n".join(ResourceReader._extract_doc_text(item) for item in payload)
        if isinstance(payload, dict):
            preferred = []
            for key in ("markdown", "content", "text", "result", "data", "body"):
                if key in payload:
                    value = ResourceReader._extract_doc_text(payload[key])
                    if value:
                        preferred.append(value)
            if preferred:
                return "\n".join(preferred)
        return ""

    async def read_dingtalk_doc(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if path.startswith("/i/p/"):
            return "该链接是钉钉文档短分享链接，DWS 不支持直接读取。"
        if path.startswith("/spreadsheetv2/"):
            return "该链接是钉钉在线表格，不按普通文档读取。"
        if path.startswith("/i/nodes/"):
            info = await self._dws_doc_call(["doc", "info", "--node", url])
            extension = (self._find_value(info, {"extension"}) or "").lower()
            if extension != "adoc":
                return "该钉钉链接不是可按 Markdown 读取的普通文档。"
            node = self._find_value(info, {"nodeid", "node_id"}) or url
        elif path.startswith("/document/edit") or path.startswith("/document/preview"):
            query = urllib.parse.parse_qs(parsed.query)
            if not query.get("dentryKey") and not query.get("dentrykey"):
                return "该钉钉文档链接缺少 dentryKey，无法安全读取。"
            node = url
        else:
            return "暂不支持这种钉钉文档链接格式。"
        payload = await self._dws_doc_call(["doc", "read", "--node", str(node)])
        text = self._extract_doc_text(payload)
        return text[: self.max_web_bytes]

    async def _dws_doc_call(self, args: List[str]) -> Dict[str, Any]:
        try:
            return await self.dws.run_json(args, timeout=45)
        except DwsError:
            return await self.dws.run_json(args + ["--verbose"], timeout=45)

    @staticmethod
    def _find_value(value: Any, keys: set) -> Optional[str]:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in keys and isinstance(child, (str, int)):
                    return str(child)
                found = ResourceReader._find_value(child, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = ResourceReader._find_value(child, keys)
                if found:
                    return found
        return None

    def _fetch_public_sync(self, url: str) -> str:
        _validate_public_url(url)
        opener = urllib.request.build_opener(_SafeRedirect())
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "dws-auto-reply/0.1 (+local read-only fetcher)",
                "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1",
            },
            method="GET",
        )
        with opener.open(request, timeout=self.web_timeout) as response:
            content_type = response.headers.get_content_type().lower()
            if content_type not in {
                "text/html",
                "text/plain",
                "text/markdown",
                "application/json",
                "application/xml",
                "text/xml",
            }:
                raise ValueError("unsupported public page content type: %s" % content_type)
            data = response.read(self.max_web_bytes + 1)
            if len(data) > self.max_web_bytes:
                data = data[: self.max_web_bytes]
            charset = response.headers.get_content_charset() or "utf-8"
            decoded = data.decode(charset, "replace")
            if content_type == "text/html":
                parser = _TextExtractor()
                parser.feed(decoded)
                decoded = parser.text()
            elif content_type == "application/json":
                try:
                    decoded = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    pass
            return html.unescape(decoded)[: self.max_web_bytes]

    async def read_links(self, urls: List[str]) -> str:
        sections = []
        for url in urls[:3]:
            try:
                if "alidocs.dingtalk.com" in urllib.parse.urlparse(url).netloc.lower():
                    content = await self.read_dingtalk_doc(url)
                    kind = "DingTalk document"
                else:
                    content = await asyncio.to_thread(self._fetch_public_sync, url)
                    kind = "public web page"
                sections.append("[%s: %s]\n%s" % (kind, url, content))
            except (DwsError, ValueError, OSError, urllib.error.URLError) as exc:
                LOGGER.warning("read-only URL fetch failed host=%s error=%s", urllib.parse.urlparse(url).hostname, exc)
                sections.append("[无法只读访问链接 %s]" % url)
        return "\n\n".join(sections)

    async def download_images(self, message: Message) -> List[str]:
        if message.content_type != "image" or not message.resource_ids:
            return []
        token = uuid.uuid4().hex
        relative_dir = "resources/%s" % token
        target = self.runtime / relative_dir
        target.mkdir(parents=True, exist_ok=False)
        files: List[str] = []
        try:
            for resource_id in message.resource_ids[:3]:
                await self.dws.run_json(
                    [
                        "chat",
                        "+messages-resource-download",
                        "--resource-id",
                        resource_id,
                        "--message-id",
                        message.message_id,
                        "--open-conversation-id",
                        message.conversation_id,
                        "--output",
                        relative_dir + "/",
                    ],
                    timeout=45,
                    cwd=str(self.runtime),
                )
            for path in target.rglob("*"):
                if not path.is_file():
                    continue
                if path.stat().st_size > self.max_image_bytes:
                    continue
                mime, _ = mimetypes.guess_type(str(path))
                if mime and mime.startswith("image/"):
                    files.append(str(path))
            return files
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def cleanup_images(self, paths: List[str]) -> None:
        parents = set()
        for value in paths:
            path = Path(value).resolve()
            if self.runtime in path.parents:
                for parent in path.parents:
                    if parent.parent == self.runtime / "resources":
                        parents.add(parent)
                        break
        for parent in parents:
            shutil.rmtree(parent, ignore_errors=True)
