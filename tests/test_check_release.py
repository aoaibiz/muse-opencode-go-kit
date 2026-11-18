import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, "scripts", "check_release.py")
# 検査対象の文字列そのものは、このファイルに直書きすると公開検査が自分を検出するため、組み立てて作る。
_HDR = "-----BEGIN " + "PRIVATE " + "KEY-----"
_FTR = "-----END " + "PRIVATE " + "KEY-----"
DUMMY_KEY = _HDR + "\nダミー本文\n" + _FTR + "\n"


def run_check(root):
    return subprocess.run(
        [sys.executable, CHECK, "--root", root],
        capture_output=True, text=True, encoding="utf-8",
    )


class CheckReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mwk-check-")
        self.kit = os.path.join(self.tmp, "kit")
        shutil.copytree(
            ROOT, self.kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_copy_passes(self):
        r = run_check(self.kit)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_secret_in_txt_is_caught(self):
        with open(os.path.join(self.kit, "docs", "dummy.txt"), "w", encoding="utf-8") as f:
            f.write(DUMMY_KEY)
        r = run_check(self.kit)
        self.assertEqual(r.returncode, 1)
        self.assertIn("private-key", r.stdout)

    def test_secret_in_yaml_is_caught(self):
        with open(os.path.join(self.kit, "docs", "dummy.yaml"), "w", encoding="utf-8") as f:
            f.write("key: |\n  " + DUMMY_KEY.replace("\n", "\n  "))
        r = run_check(self.kit)
        self.assertEqual(r.returncode, 1)
        self.assertIn("private-key", r.stdout)

    def test_secret_in_extensionless_file_is_caught(self):
        with open(os.path.join(self.kit, "templates", "dummy"), "w", encoding="utf-8") as f:
            f.write(DUMMY_KEY)
        r = run_check(self.kit)
        self.assertEqual(r.returncode, 1)
        self.assertIn("private-key", r.stdout)


if __name__ == "__main__":
    unittest.main()
