import subprocess
import tempfile
import unittest
from pathlib import Path

from app.repository_reader import RepositoryReader


class RepositoryReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "sample-repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Tests")
        (self.repo / "README.md").write_text("MAIN_ONLY\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "main")

        self._git("switch", "-c", "feature/read-only")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "service.py").write_text(
            "BRANCH_ONLY = 'readable'\n", encoding="utf-8"
        )
        self._git("add", "src/service.py")
        self._git("commit", "-m", "feature")
        feature_oid = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("update-ref", "refs/remotes/origin/feature/read-only", feature_oid)
        self.remote = Path(self.temp.name) / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(self.repo), str(self.remote)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "update-ref",
                "refs/heads/remote-only",
                feature_oid,
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._git("remote", "add", "origin", str(self.remote))
        self._git("switch", "main")
        (self.repo / "working.txt").write_text("WORKTREE_ONLY\n", encoding="utf-8")

        self.reader = RepositoryReader.__new__(RepositoryReader)
        self.reader.root = self.repo
        self.reader.max_bytes = 49152
        self.reader.timeout_seconds = 10.0
        self.reader.fetch_timeout_seconds = 10.0
        self.reader.branch_batch_size = 8
        self.reader.remote = "origin"
        self.reader.allow_fetch = False
        self.reader.allowed_paths = (".",)
        self.reader._ref_cache = (0.0, [])

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    async def test_explicit_branch_is_read_without_switching_worktree(self):
        before_branch = self._git("branch", "--show-current").stdout
        before_status = self._git("status", "--porcelain").stdout
        result = await self.reader.scan(
            "请看 feature/read-only 分支的 src/service.py 里 BRANCH_ONLY"
        )
        self.assertIn("BRANCH_ONLY = 'readable'", result)
        self.assertIn("分支 feature/read-only", result)
        self.assertEqual(before_branch, self._git("branch", "--show-current").stdout)
        self.assertEqual(before_status, self._git("status", "--porcelain").stdout)

    async def test_current_worktree_includes_uncommitted_files(self):
        result = await self.reader.scan("WORKTREE_ONLY 在哪里")
        self.assertIn("当前工作树（包含未提交改动）", result)
        self.assertIn("working.txt", result)

    async def test_lists_local_and_remote_tracking_refs(self):
        result = await self.reader.scan("列出所有分支")
        self.assertIn("local: main", result)
        self.assertIn("local: feature/read-only", result)
        self.assertIn("remote: origin/feature/read-only", result)

    async def test_mutating_git_subcommands_are_rejected(self):
        for command in ("checkout", "switch", "fetch", "pull", "worktree"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await self.reader._git([command])

    async def test_explicit_remote_branch_is_fetched_on_demand(self):
        self.reader.allow_fetch = True
        before = self._git("branch", "--show-current").stdout
        result = await self.reader.scan("请看 remote-only 分支的 BRANCH_ONLY")
        self.assertIn("指定分支已按需更新", result)
        self.assertIn("BRANCH_ONLY", result)
        self.assertEqual(before, self._git("branch", "--show-current").stdout)

    async def test_allowed_directory_restricts_worktree_and_branch_reads(self):
        self.reader.allowed_paths = ("src",)
        self.assertIn("没有找到", await self.reader.scan("MAIN_ONLY"))
        result = await self.reader.scan("feature/read-only 分支的 src/service.py BRANCH_ONLY")
        self.assertIn("BRANCH_ONLY", result)
        self.assertFalse(self.reader._path_is_allowed("README.md"))

    def test_search_prioritizes_schema_identifiers_and_redacts_secrets(self):
        question = (
            "测试用户甲，sample_order_link 这张表和 workspace_id、purchase_id、tenant_id 是什么关系？"
        )
        patterns = self.reader._patterns(question)
        self.assertEqual(
            ["sample_order_link", "workspace_id", "purchase_id", "tenant_id"],
            patterns[:4],
        )
        redacted = self.reader._redact_sensitive_text(
            'email_key = "should-never-be-returned"\npurchase_id = value'
        )
        self.assertIn("email_key=<REDACTED>", redacted)
        self.assertNotIn("should-never-be-returned", redacted)
        self.assertIn("purchase_id = value", redacted)


if __name__ == "__main__":
    unittest.main()
