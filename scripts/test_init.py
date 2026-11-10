"""用临时独立状态核首次配对：默认不泄露、显式显示、不轮换既有密钥。"""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.env = dict(os.environ, WBAGENT_STATE=str(self.root))
        self.command = [sys.executable, "-B", str(Path(__file__).resolve().with_name("wbagent.py")), "init"]

    def invoke(self, *args):
        return subprocess.run([*self.command, *args], env=self.env, capture_output=True, text=True, check=True)

    def test_default_init_creates_private_secret_without_display(self):
        result = self.invoke()
        secret_file = self.root / "secret"
        secret = secret_file.read_text().strip()
        self.assertRegex(secret, r"^[a-f0-9]{32}$")
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn("WBAGENT_INITIALIZED", result.stdout)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)

    def test_show_secret_is_explicit_and_preserves_existing_pairing(self):
        self.invoke()
        secret = (self.root / "secret").read_text()
        result = self.invoke("--show-secret")
        self.assertEqual(result.stdout, secret)
        self.assertEqual((self.root / "secret").read_text(), secret)
        self.assertEqual(result.stderr, "")

    def test_repeated_default_init_neither_displays_nor_rotates_secret(self):
        self.invoke()
        secret = (self.root / "secret").read_bytes()
        result = self.invoke()
        self.assertEqual((self.root / "secret").read_bytes(), secret)
        self.assertNotIn(secret.decode().strip(), result.stdout + result.stderr)

    def test_concurrent_initialization_does_not_overwrite_pairing(self):
        self.invoke()
        secret = (self.root / "secret").read_bytes()
        processes = [subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertNotIn(secret.decode().strip(), out + err)
        self.assertEqual((self.root / "secret").read_bytes(), secret)


if __name__ == "__main__":
    unittest.main(verbosity=2)
