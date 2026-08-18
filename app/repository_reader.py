from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{2,}|[\u4e00-\u9fff]{2,8}")
ALL_BRANCHES_RE = re.compile(
    r"(?:所有|全部|每个)(?:本地|远端|远程)?分支|(?:all|every)\s+(?:git\s+)?branches?",
    re.IGNORECASE,
)
LIST_BRANCHES_RE = re.compile(
    r"(?:列出|查看|看看|有哪些|多少)(?:一下)?(?:所有|全部|本地|远端|远程)?分支|"
    r"分支(?:列表|清单)|list\s+(?:all\s+)?(?:git\s+)?branches?",
    re.IGNORECASE,
)
STOP_WORDS = {
    "什么",
    "怎么",
    "如何",
    "一下",
    "这个",
    "那个",
    "问题",
    "代码",
    "数据库",
    "帮忙",
    "看看",
    "分支",
    "所有分支",
    "全部分支",
    "please",
    "could",
    "would",
    "about",
    "branch",
    "branches",
}
READ_ONLY_GIT_SUBCOMMANDS = frozenset({"for-each-ref", "grep", "show"})
BRANCH_HINT_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9._/-])([A-Za-z0-9][A-Za-z0-9._/-]{0,199})\s*分支", re.I),
    re.compile(
        r"(?:分支|branch)\s*(?:是|为|[:：])?\s*([A-Za-z0-9][A-Za-z0-9._/-]{0,199})",
        re.I,
    ),
)
EXCLUDED_PATHSPECS = (
    ":(exclude).env",
    ":(exclude).env.*",
    ":(exclude)**/.env",
    ":(exclude)**/.env.*",
    ":(exclude)**/*.pem",
    ":(exclude)**/*.key",
    ":(exclude)**/credentials*",
    ":(exclude)**/node_modules/**",
    ":(exclude)**/.venv/**",
    ":(exclude)**/dist/**",
    ":(exclude)**/build/**",
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?P<label>(?:api[_-]?key|access[_-]?token|secret|password|passwd|private[_-]?key|"
    r"email[_-]?key|邮箱(?:key|密钥|密码)))\s*(?P<operator>[:=])\s*"
    r"(?P<value>[^\s,;}\]]{8,}|['\"][^'\"\n]+['\"])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GitRef:
    full_name: str
    object_id: str

    @property
    def display_name(self) -> str:
        if self.full_name.startswith("refs/heads/"):
            return self.full_name.removeprefix("refs/heads/")
        return self.full_name.removeprefix("refs/remotes/")

    @property
    def aliases(self) -> tuple[str, ...]:
        names = [self.display_name]
        if self.full_name.startswith("refs/remotes/") and "/" in self.display_name:
            names.append(self.display_name.split("/", 1)[1])
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    timed_out: bool = False


class RepositoryReader:
    def __init__(self, config: Dict[str, Any]):
        self._ref_cache: tuple[float, List[GitRef]] = (0.0, [])
        self.update_config(config)

    def update_config(self, config: Dict[str, Any]) -> None:
        section = config["repository"]
        self.root = Path(section["path"]).expanduser().resolve()
        self.max_bytes = int(section.get("max_scan_bytes", 49152))
        self.timeout_seconds = float(section.get("scan_timeout_seconds", 20))
        self.fetch_timeout_seconds = float(section.get("fetch_timeout_seconds", 20))
        self.branch_batch_size = int(section.get("branch_scan_batch_size", 32))
        self.remote = str(section.get("remote", "origin"))
        self.allow_fetch = bool(section.get("allow_fetch", False))
        allowed_paths = [str(value).strip().strip("/") or "." for value in section["allowed_paths"]]
        self.allowed_paths = (".",) if "." in allowed_paths else tuple(allowed_paths)
        if section.get("read_only") is not True or section.get("database_access") is not False:
            raise ValueError("unsafe repository configuration")
        if (
            section.get("read_local_git_refs") is not True
            or section.get("allow_checkout") is not False
        ):
            raise ValueError("repository checkout must remain disabled")

    def _patterns(self, question: str) -> List[str]:
        values = []
        for index, token in enumerate(TOKEN_RE.findall(question)):
            normalized = token.strip("./:-_")
            if len(normalized) < 2 or normalized.lower() in STOP_WORDS:
                continue
            if any(item[2] == normalized for item in values):
                continue
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[_.:/-][A-Za-z0-9]+)+", normalized):
                priority = 0
            elif normalized.isascii():
                priority = 1
            else:
                priority = 2
            values.append((priority, index, normalized))
        return [item[2] for item in sorted(values)[:12]]

    @staticmethod
    def _redact_sensitive_text(content: str) -> str:
        return SENSITIVE_ASSIGNMENT_RE.sub(
            lambda match: "%s%s<REDACTED>"
            % (match.group("label"), match.group("operator")),
            content,
        )

    @staticmethod
    async def _drain(
        stream: asyncio.StreamReader, limit: int, limit_event: asyncio.Event | None = None
    ) -> tuple[bytes, bool]:
        kept = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            remaining = max(0, limit - len(kept))
            if remaining:
                kept.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                if limit_event is not None:
                    limit_event.set()
        return bytes(kept), truncated

    async def _run_command(
        self, command: Sequence[str], max_bytes: int, timeout: float
    ) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.root),
                env=os.environ.copy(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CommandResult(127, b"", str(exc).encode("utf-8", "replace"))

        limit_event = asyncio.Event()
        stdout_task = asyncio.create_task(
            self._drain(process.stdout, max_bytes, limit_event),
            name="repository-reader-stdout",
        )
        stderr_task = asyncio.create_task(
            self._drain(process.stderr, 4096), name="repository-reader-stderr"
        )
        wait_task = asyncio.create_task(process.wait(), name="repository-reader-process")
        limit_task = asyncio.create_task(
            limit_event.wait(), name="repository-reader-limit"
        )
        timed_out = False
        stopped_for_limit = False
        done, _ = await asyncio.wait(
            {wait_task, limit_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            timed_out = True
        elif limit_task in done and limit_event.is_set() and not wait_task.done():
            stopped_for_limit = True

        if timed_out or stopped_for_limit:
            process.terminate()
            try:
                await asyncio.wait_for(wait_task, timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await wait_task
        else:
            await wait_task
        if not limit_task.done():
            limit_task.cancel()
        await asyncio.gather(limit_task, return_exceptions=True)
        stdout, stream_truncated = await stdout_task
        stderr, _ = await stderr_task
        return CommandResult(
            process.returncode,
            stdout,
            stderr,
            truncated=stream_truncated or stopped_for_limit,
            timed_out=timed_out,
        )

    async def _git(
        self, args: Sequence[str], max_bytes: int | None = None, timeout: float | None = None
    ) -> CommandResult:
        if not args or args[0] not in READ_ONLY_GIT_SUBCOMMANDS:
            raise ValueError("non-read-only Git subcommand rejected")
        return await self._run_command(
            ["git", "--no-pager", *args],
            max_bytes=max_bytes or self.max_bytes,
            timeout=timeout or self.timeout_seconds,
        )

    @staticmethod
    def _valid_branch_name(value: str) -> bool:
        return bool(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value)
            and ".." not in value
            and "@{" not in value
            and "//" not in value
            and not value.endswith(("/", ".", ".lock"))
        )

    def _branch_hint(self, question: str) -> str:
        for pattern in BRANCH_HINT_PATTERNS:
            match = pattern.search(question)
            if not match:
                continue
            value = match.group(1).strip("./")
            remote_prefix = self.remote + "/"
            if value.startswith(remote_prefix):
                value = value[len(remote_prefix) :]
            if self._valid_branch_name(value):
                return value
        return ""

    async def _fetch_branch(self, branch: str) -> tuple[bool, str]:
        if not self.allow_fetch or not branch:
            return False, ""
        destination = f"refs/remotes/{self.remote}/{branch}"
        result = await self._run_command(
            [
                "git",
                "--no-pager",
                "fetch",
                "--no-tags",
                self.remote,
                f"{branch}:{destination}",
            ],
            max_bytes=4096,
            timeout=self.fetch_timeout_seconds,
        )
        self._ref_cache = (0.0, [])
        if result.returncode == 0 and not result.timed_out:
            return True, f"[指定分支已按需更新: {self.remote}/{branch}]"
        return False, "[指定分支更新失败，已回退到本地已有引用]"

    async def _refs(self) -> List[GitRef]:
        cached_at, cached = self._ref_cache
        if cached and time.monotonic() - cached_at < 60:
            return cached
        result = await self._git(
            [
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                "refs/heads",
                "refs/remotes",
            ],
            max_bytes=2_000_000,
            timeout=min(10.0, self.timeout_seconds),
        )
        refs = []
        if result.returncode == 0 or result.stdout:
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                try:
                    name, object_id = line.split("\t", 1)
                except ValueError:
                    continue
                if name.endswith("/HEAD"):
                    continue
                if name.startswith(("refs/heads/", "refs/remotes/")):
                    refs.append(GitRef(name, object_id))
        self._ref_cache = (time.monotonic(), refs)
        return refs

    @staticmethod
    def _alias_is_mentioned(question: str, alias: str) -> bool:
        branch_chars = r"A-Za-z0-9._/-"
        return bool(
            re.search(
                rf"(?<![{branch_chars}]){re.escape(alias)}(?![{branch_chars}])",
                question,
                re.IGNORECASE,
            )
        )

    def _selected_refs(self, question: str, refs: Sequence[GitRef]) -> tuple[List[GitRef], bool]:
        if ALL_BRANCHES_RE.search(question):
            return list(refs), True
        has_branch_cue = bool(
            re.search(r"分支|branches?|refs/(?:heads|remotes)/", question, re.IGNORECASE)
        )
        selected = []
        for ref in refs:
            aliases = sorted(ref.aliases, key=len, reverse=True)
            if any(
                (has_branch_cue or "/" in alias)
                and self._alias_is_mentioned(question, alias)
                for alias in aliases
            ):
                selected.append(ref)
        return self._deduplicate_tips(selected), False

    @staticmethod
    def _deduplicate_tips(refs: Sequence[GitRef]) -> List[GitRef]:
        result = []
        seen = set()
        for ref in refs:
            if ref.object_id in seen:
                continue
            seen.add(ref.object_id)
            result.append(ref)
        return result

    @staticmethod
    def _is_sensitive_path(path: str) -> bool:
        lowered = path.lower()
        name = PurePosixPath(lowered).name
        return (
            name == ".env"
            or name.startswith(".env.")
            or name.startswith("credentials")
            or lowered.endswith((".pem", ".key"))
            or any(
                part in {"node_modules", ".venv", "dist", "build"}
                for part in PurePosixPath(lowered).parts
            )
        )

    def _path_is_allowed(self, path: str) -> bool:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            return False
        if self.allowed_paths == (".",):
            return True
        return any(
            candidate == PurePosixPath(root)
            or PurePosixPath(root) in candidate.parents
            for root in self.allowed_paths
        )

    def _path_candidates(self, question: str, refs: Sequence[GitRef]) -> List[str]:
        branch_aliases = {alias.lower() for ref in refs for alias in ref.aliases}
        candidates = []
        for token in TOKEN_RE.findall(question):
            value = re.sub(r":\d+(?::\d+)?$", "", token.strip("`'\".,，。()[]{}"))
            if not value or value.lower() in branch_aliases:
                continue
            if value.startswith(("/", "http:", "https:")) or ".." in PurePosixPath(value).parts:
                continue
            basename = PurePosixPath(value).name
            looks_like_file = "." in basename or basename.lower() in {
                "dockerfile",
                "makefile",
                "procfile",
                "readme",
            }
            if (
                not looks_like_file
                or self._is_sensitive_path(value)
                or not self._path_is_allowed(value)
            ):
                continue
            if value not in candidates:
                candidates.append(value)
        return candidates[:3]

    async def _read_named_files(self, question: str, refs: Sequence[GitRef]) -> str:
        if not refs or len(refs) > 8:
            return ""
        paths = self._path_candidates(question, refs)
        if not paths:
            return ""
        sections = []
        remaining = self.max_bytes
        for ref in refs:
            for path in paths:
                if remaining <= 0:
                    break
                result = await self._git(
                    ["show", "--no-ext-diff", f"{ref.full_name}:{path}"],
                    max_bytes=remaining,
                    timeout=min(8.0, self.timeout_seconds),
                )
                if result.returncode != 0 or not result.stdout or b"\x00" in result.stdout:
                    continue
                content = self._redact_sensitive_text(
                    result.stdout.decode("utf-8", "replace")
                )
                section = f"[分支 {ref.display_name} 文件 {path}]\n{content}"
                sections.append(section)
                remaining -= len(section.encode("utf-8"))
                if result.truncated:
                    sections.append("[文件内容已按安全上限截断]")
                    remaining = 0
                    break
        return "\n".join(sections)

    async def _scan_worktree(self, expression: str) -> CommandResult:
        roots = [
            value
            for value in self.allowed_paths
            if value == "." or (self.root / value).exists()
        ]
        if not roots:
            return CommandResult(1, b"", b"")
        command = [
            "rg",
            "-n",
            "-i",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            "8",
            "-C",
            "3",
            "--glob",
            "!.git/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!dist/**",
            "--glob",
            "!build/**",
            "--glob",
            "!.env",
            "--glob",
            "!.env.*",
            "--glob",
            "!**/*.pem",
            "--glob",
            "!**/*.key",
            "--glob",
            "!**/credentials*",
            expression,
            *roots,
        ]
        return await self._run_command(command, self.max_bytes, self.timeout_seconds)

    async def _scan_refs(self, expression: str, refs: Sequence[GitRef]) -> CommandResult:
        refs = self._deduplicate_tips(refs)
        output = bytearray()
        errors = bytearray()
        truncated = False
        timed_out = False
        deadline = time.monotonic() + self.timeout_seconds
        for offset in range(0, len(refs), self.branch_batch_size):
            remaining_time = deadline - time.monotonic()
            remaining_bytes = self.max_bytes - len(output)
            if remaining_time <= 0:
                timed_out = True
                break
            if remaining_bytes <= 0:
                truncated = True
                break
            batch = refs[offset : offset + self.branch_batch_size]
            result = await self._git(
                [
                    "grep",
                    "-n",
                    "-i",
                    "-I",
                    "-E",
                    "--no-color",
                    "--max-count",
                    "8",
                    "-C",
                    "3",
                    "-e",
                    expression,
                    *[ref.full_name for ref in batch],
                    "--",
                    *self.allowed_paths,
                    *EXCLUDED_PATHSPECS,
                ],
                max_bytes=remaining_bytes,
                timeout=remaining_time,
            )
            output.extend(result.stdout)
            if result.stderr and len(errors) < 4096:
                errors.extend(result.stderr[: 4096 - len(errors)])
            if result.truncated:
                truncated = True
                break
            if result.timed_out:
                timed_out = True
                break
        return CommandResult(0, bytes(output), bytes(errors), truncated, timed_out)

    def _format_ref_list(self, refs: Sequence[GitRef]) -> str:
        local = [ref.display_name for ref in refs if ref.full_name.startswith("refs/heads/")]
        remote = [ref.display_name for ref in refs if ref.full_name.startswith("refs/remotes/")]
        header = f"配置仓库当前可访问 {len(refs)} 个分支引用（本地 {len(local)}，远端跟踪 {len(remote)}）。"
        content = header + "\n" + "\n".join([*(f"local: {name}" for name in local), *(f"remote: {name}" for name in remote)])
        encoded = content.encode("utf-8")
        if len(encoded) <= self.max_bytes:
            return content
        return encoded[: self.max_bytes].decode("utf-8", "ignore") + "\n[分支列表已按安全上限截断]"

    async def scan(self, question: str) -> str:
        if not self.root.is_dir() or not (self.root / ".git").exists():
            return "配置的 Git 仓库不存在，无法执行只读扫描。"

        refs = await self._refs()
        branch_hint = self._branch_hint(question)
        fetch_note = ""
        if branch_hint:
            _, fetch_note = await self._fetch_branch(branch_hint)
            refs = await self._refs()
        if LIST_BRANCHES_RE.search(question):
            return self._format_ref_list(refs)

        patterns = self._patterns(question)
        if not patterns:
            return "没有提取到可用于只读代码搜索的关键词。"
        expression = "|".join(re.escape(item) for item in patterns)
        selected, all_branches = self._selected_refs(question, refs)
        if branch_hint and not selected:
            note = (fetch_note + "\n") if fetch_note else ""
            return note + "指定分支在本地和远端跟踪引用中都不存在，无法读取。"

        named_files = ""
        if selected and not all_branches:
            named_files = await self._read_named_files(question, selected)

        if selected:
            result = await self._scan_refs(expression, selected)
            source = "所有本地已有分支引用" if all_branches else ", ".join(
                ref.display_name for ref in selected
            )
        else:
            result = await self._scan_worktree(expression)
            source = "当前工作树（包含未提交改动）"

        if result.returncode not in (0, 1) and not result.stdout:
            detail = result.stderr.decode("utf-8", "replace")[:500]
            return "只读代码搜索失败：%s" % detail

        content = self._redact_sensitive_text(result.stdout.decode("utf-8", "replace"))
        parts = [fetch_note, f"[只读来源: {source}]", named_files, content]
        if result.truncated:
            parts.append("[结果已按安全上限截断，不代表完整搜索结果]")
        if result.timed_out:
            parts.append("[跨分支搜索已达到时间上限，不代表完整搜索结果]")
        answer = "\n".join(part for part in parts if part.strip())
        if answer.strip() == f"[只读来源: {source}]":
            return f"在 {source} 中没有找到相关内容。"
        return answer
