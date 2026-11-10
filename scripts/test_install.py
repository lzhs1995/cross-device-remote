"""隔离用户目录验证安装/检查/备份，不读取或改写当前用户的配置。"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parent.parent


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "isolated home 測試"
        # Windows 未授予 symlink 权限时如实跳过，不能要求管理员或改真实设置。
        target = self.base / "target"
        target.mkdir()
        try:
            (self.base / "probe").symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink capability unavailable in this test process: {exc}")

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", str(SOURCE / "scripts/install.py"),
                               "--prefix", str(self.home), *args], capture_output=True, text=True, encoding="utf-8")

    def config(self):
        return self.home / ".config/cross-device-remote/config.json"

    def test_fresh_install_links_all_agents_without_starting_services(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        for base in (".agents/skills", ".codex/skills", ".claude/skills", ".gemini/skills", ".gemini/antigravity/skills"):
            link = self.home / base / "cross-device-remote"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), SOURCE)
        config = json.loads(self.config().read_text(encoding="utf-8"))
        self.assertEqual(config["state_dir"], str(self.home / ".local/state/cross-device-remote"))
        self.assertEqual(config["serve_args"][config["serve_args"].index("--host") + 1], "127.0.0.1")
        self.assertFalse(receipt["services_started"])
        self.assertFalse(Path(config["state_dir"]).exists())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.config().stat().st_mode), 0o600)

    def test_existing_private_config_is_unchanged_on_repeat_install(self):
        self.config().parent.mkdir(parents=True)
        original = b'{"private_fixture": "retain exactly"}\n'
        self.config().write_bytes(original)
        for _ in range(2):
            result = self.invoke("--agents", "codex", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["config_created"])
            self.assertEqual(self.config().read_bytes(), original)

    def test_conflict_refuses_before_installing_other_links(self):
        conflict = self.home / ".codex/skills/cross-device-remote"
        conflict.mkdir(parents=True)
        (conflict / "owned.txt").write_text("preserve", encoding="utf-8")
        result = self.invoke("--agents", "codex", "claude")
        self.assertEqual(result.returncode, 2)
        self.assertIn("XDEV_INSTALL_REFUSED", result.stderr)
        self.assertEqual((conflict / "owned.txt").read_text(encoding="utf-8"), "preserve")
        self.assertFalse((self.home / ".agents").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse(self.config().exists())

    def test_replace_keeps_original_directory_in_reported_backup(self):
        conflict = self.home / ".agents/skills/cross-device-remote"
        conflict.mkdir(parents=True)
        (conflict / "owned.txt").write_bytes(b"original\x00bytes")
        result = self.invoke("--replace", "--agents", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(len(receipt["backups"]), 1)
        backup = Path(receipt["backups"][0])
        self.assertEqual((backup / "owned.txt").read_bytes(), b"original\x00bytes")
        self.assertEqual(conflict.resolve(), SOURCE)

    def test_check_missing_install_does_not_create_prefix(self):
        result = self.invoke("--check", "--agents", "codex")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertFalse(self.home.exists())

    def test_check_conflict_reports_mismatch_without_replacement(self):
        conflict = self.home / ".codex/skills/cross-device-remote"
        conflict.mkdir(parents=True)
        (conflict / "owned.txt").write_text("preserve", encoding="utf-8")
        result = self.invoke("--check", "--replace", "--agents", "codex")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual((conflict / "owned.txt").read_text(encoding="utf-8"), "preserve")
        self.assertFalse((self.home / ".agents").exists())

    def test_dangling_private_config_symlink_is_preserved(self):
        self.config().parent.mkdir(parents=True)
        target = self.home / "private-profile-not-mounted.json"
        self.config().symlink_to(target)
        result = self.invoke("--agents", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["config_created"])
        self.assertTrue(self.config().is_symlink())
        self.assertEqual(self.config().resolve(), target)
        self.assertFalse(target.exists())

    def test_selected_agents_and_readonly_check(self):
        result = self.invoke("--agents", "codex", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["agents"], ["codex"])
        self.assertFalse((self.home / ".claude").exists())
        before = self.config().read_bytes()
        check = self.invoke("--check", "--agents", "codex")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(self.config().read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
