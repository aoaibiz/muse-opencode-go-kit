import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "scripts", "install.py")


def run_install(*args):
    return subprocess.run(
        [sys.executable, INSTALL] + list(args),
        capture_output=True, text=True, encoding="utf-8",
    )


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mwk-test-")
        self.target = os.path.join(self.tmp, "target")
        os.makedirs(self.target)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply(self):
        r = run_install("apply", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_plan_makes_no_changes(self):
        r = run_install("plan", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.target), [])

    def test_apply_installs_block_and_skills(self):
        self.apply()
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("muse-window-kit:start", text)
        self.assertIn("窓口エージェントの運用ルール", text)
        skill = os.path.join(self.target, "skills", "dashboard-build", "SKILL.md")
        self.assertTrue(os.path.isfile(skill))

    def test_apply_preserves_existing_agents_bytes(self):
        original = "# 既存のルール\n\n元の本文です。\n"
        with open(os.path.join(self.target, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write(original)
        self.apply()
        with open(os.path.join(self.target, "AGENTS.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith(original))
        self.assertIn("muse-window-kit:start", text)

    def test_apply_twice_is_idempotent(self):
        self.apply()
        with open(os.path.join(self.target, "AGENTS.md"), "rb") as f:
            first = f.read()
        self.apply()
        with open(os.path.join(self.target, "AGENTS.md"), "rb") as f:
            second = f.read()
        self.assertEqual(first, second)
        self.assertEqual(second.decode("utf-8").count("muse-window-kit:start"), 1)

    def test_verify_passes_then_fails_after_tamper(self):
        self.apply()
        r = run_install("verify", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        skill = os.path.join(self.target, "skills", "dashboard-build", "SKILL.md")
        with open(skill, "a", encoding="utf-8") as f:
            f.write("\n改ざん\n")
        r = run_install("verify", "--target", self.target)
        self.assertEqual(r.returncode, 1)

    def test_rollback_restores_original_and_removes_skills(self):
        original = "# 既存\n\n戻るべき本文。\n"
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, "w", encoding="utf-8") as f:
            f.write(original)
        self.apply()
        r = run_install("rollback", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(agents, encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertFalse(os.path.exists(os.path.join(self.target, "skills", "dashboard-build", "SKILL.md")))

    def test_rollback_on_fresh_target_removes_created_agents(self):
        self.apply()
        r = run_install("rollback", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.target, "AGENTS.md")))

    def test_local_edit_conflict_blocks_apply(self):
        self.apply()
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("窓口エージェントの運用ルール", "手で書き換えたルール")
        with open(agents, "w", encoding="utf-8") as f:
            f.write(text)
        r = run_install("apply", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LOCAL_EDIT_CONFLICT", r.stderr)

    def test_checksum_mismatch_refused(self):
        fake_kit = os.path.join(self.tmp, "fake-kit")
        shutil.copytree(os.path.join(ROOT, "payload"), os.path.join(fake_kit, "payload"))
        shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(fake_kit, "scripts"))
        with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["files"]["AGENTS.md"] = "0" * 64
        with open(os.path.join(fake_kit, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        r = subprocess.run(
            [sys.executable, os.path.join(fake_kit, "scripts", "install.py"),
             "apply", "--target", self.target, "--kit", fake_kit],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("CHECKSUM_MISMATCH", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.target, "AGENTS.md")))

    def _set_status_partial(self):
        state_path = os.path.join(self.target, ".muse-window-kit", "state.json")
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        state["status"] = "PARTIAL"
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def test_recover_restores_after_partial_without_edits(self):
        original = "# 既存\n\n復旧する本文。\n"
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, "w", encoding="utf-8") as f:
            f.write(original)
        self.apply()
        self._set_status_partial()
        r = run_install("recover", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(agents, encoding="utf-8") as f:
            self.assertEqual(f.read(), original)

    def test_recover_stops_when_edited_after_partial(self):
        original = "# 既存\n\n復旧する本文。\n"
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, "w", encoding="utf-8") as f:
            f.write(original)
        self.apply()
        self._set_status_partial()
        with open(agents, "a", encoding="utf-8") as f:
            f.write("\n導入後の追記\n")
        with open(agents, encoding="utf-8") as f:
            edited = f.read()
        r = run_install("recover", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LOCAL_EDIT_CONFLICT", r.stderr)
        with open(agents, encoding="utf-8") as f:
            self.assertEqual(f.read(), edited)

    def test_rollback_stops_when_edited_after_install(self):
        self.apply()
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, "a", encoding="utf-8") as f:
            f.write("\nブロック外への追記\n")
        with open(agents, encoding="utf-8") as f:
            edited = f.read()
        r = run_install("rollback", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LOCAL_EDIT_CONFLICT", r.stderr)
        with open(agents, encoding="utf-8") as f:
            self.assertEqual(f.read(), edited)

    @unittest.skipIf(os.name == "nt", "ファイルモードは POSIX 前提")
    def test_apply_preserves_file_mode(self):
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, "w", encoding="utf-8") as f:
            f.write("# 既存\n")
        os.chmod(agents, 0o640)
        self.apply()
        self.assertEqual(os.stat(agents).st_mode & 0o777, 0o640)

    def test_broken_state_record_is_structured_error(self):
        self.apply()
        state_path = os.path.join(self.target, ".muse-window-kit", "state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("{壊れた記録")
        r = run_install("verify", "--target", self.target)
        self.assertEqual(r.returncode, 0)  # verify は状態記録を読まない
        r = run_install("rollback", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("STATE_BROKEN", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_lock_blocks_apply(self):
        os.makedirs(os.path.join(self.target, ".muse-window-kit", "lock"))
        r = run_install("apply", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LOCKED", r.stderr)

    def test_non_utf8_agents_is_structured_error(self):
        agents = os.path.join(self.target, "AGENTS.md")
        with open(agents, "wb") as f:
            f.write(b"\x82\xa0\x82\xa2\x82\xa4")
        r = run_install("plan", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("ENCODING_INVALID", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipIf(os.name == "nt", "シンボリックリンクは POSIX 前提")
    def test_parent_symlink_refused(self):
        real_dir = os.path.join(self.tmp, "real-skills")
        os.makedirs(os.path.join(real_dir, "dashboard-build"))
        os.symlink(real_dir, os.path.join(self.target, "skills"))
        r = run_install("plan", "--target", self.target)
        self.assertEqual(r.returncode, 2)
        self.assertIn("SYMLINK_REFUSED", r.stderr)


if __name__ == "__main__":
    unittest.main()
