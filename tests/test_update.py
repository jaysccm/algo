"""Exercise the updater's safety transitions without a host or credentials."""

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from importlib.machinery import SourceFileLoader

update = SourceFileLoader(
    "pinchy_update", str(Path(__file__).resolve().parent.parent / "script" / "ext" / "update")
).load_module()


class UpdateTests(unittest.TestCase):
    old_ref = "1" * 40
    new_ref = "2" * 40
    expected_id = "sha256:" + "a" * 64

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "instances").mkdir()
        for name in update.INSTANCES:
            env = self.root / "instances" / f"{name}.env"
            env.write_text(f"INSTANCE={name}\n")
            os.chmod(env, 0o600)
        host_env = self.root / ".env.host"
        host_env.write_text("# test\n")
        os.chmod(host_env, 0o600)
        (self.root / "instances" / "live.HALT").write_text("halted\n")
        (self.root / ".release-image").write_text(f"algo:{self.old_ref}\n")
        self.current_ref = self.old_ref
        self.active_units = {
            "pinchy-paper-session.timer",
            "pinchy-live-backup.timer",
            "pinchy-paper-backup.timer",
        }
        self.actions = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(update, "ROOT", self.root))
        self.stack.enter_context(mock.patch.object(update, "IMAGE_FILE", self.root / ".release-image"))
        self.stack.enter_context(mock.patch.object(update, "call", side_effect=self.fake_call))
        self.stack.enter_context(mock.patch.object(update, "compose", return_value="db\ntrading\ndashboard"))

    def fake_call(self, *args, env=None, stdin=None, check=True):
        self.actions.append(args)
        if args[:2] == ("git", "rev-parse"):
            return 0, self.current_ref if args[2] == "HEAD" else self.new_ref
        if args[:2] == ("git", "status"):
            return 0, ""
        if args[:2] == ("git", "fetch"):
            return 0, ""
        if args[:2] == ("git", "ls-tree"):
            return 0, "instances/live.HALT"
        if args[:2] == ("git", "checkout"):
            self.current_ref = args[-1]
            return 0, ""
        if args[:3] == ("docker", "image", "inspect"):
            return 0, self.expected_id
        if args[:2] == ("systemctl", "is-enabled"):
            return (0, "enabled") if args[2] in self.active_units else (1, "disabled")
        if args[:2] == ("systemctl", "is-active"):
            return (0, "active") if args[2] in self.active_units else (3, "inactive")
        if args[:3] == ("sudo", "-n", "systemctl"):
            if args[3] == "stop":
                self.active_units.discard(args[4])
            else:
                self.active_units.add(args[4])
            return 0, ""
        if args[:2] == ("python3", "scripts/status.py"):
            return 0, "healthy"
        raise AssertionError(f"unexpected command: {args}")

    def test_repeat_deploy_checks_health_without_restarting_jobs(self):
        (self.root / ".release-image").write_text(f"algo:{self.new_ref}\n")
        self.current_ref = self.new_ref
        health = self.stack.enter_context(mock.patch.object(update, "healthy"))
        backup = self.stack.enter_context(mock.patch.object(update, "backup"))

        update.deploy(self.new_ref, self.expected_id)

        health.assert_called_once_with(f"algo:{self.new_ref}")
        backup.assert_not_called()
        self.assertFalse(any(args[:2] == ("sudo", "-n") for args in self.actions))

    def test_success_preserves_timer_and_halt_state(self):
        backups = self.stack.enter_context(mock.patch.object(update, "backup"))
        migrate = self.stack.enter_context(mock.patch.object(update, "migrate_and_start"))
        health = self.stack.enter_context(mock.patch.object(update, "healthy"))
        before_timers = self.active_units.copy()

        update.deploy(self.new_ref, self.expected_id)

        self.assertEqual(self.current_ref, self.new_ref)
        self.assertEqual((self.root / ".release-image").read_text().strip(), f"algo:{self.new_ref}")
        self.assertEqual(self.active_units, before_timers)
        self.assertTrue((self.root / "instances" / "live.HALT").exists())
        self.assertEqual(backups.call_count, 2)
        migrate.assert_called_once_with(f"algo:{self.new_ref}")
        health.assert_called_once_with(f"algo:{self.new_ref}")

    def test_failed_new_stack_rolls_back_before_resuming_timers(self):
        self.stack.enter_context(mock.patch.object(update, "backup"))
        self.stack.enter_context(mock.patch.object(update, "healthy"))

        def migrate(image):
            if image == f"algo:{self.new_ref}":
                raise update.DeployError("simulated health or migration failure")

        self.stack.enter_context(mock.patch.object(update, "migrate_and_start", side_effect=migrate))
        before_timers = self.active_units.copy()

        with self.assertRaisesRegex(update.DeployError, "Previous release restored"):
            update.deploy(self.new_ref, self.expected_id)

        self.assertEqual(self.current_ref, self.old_ref)
        self.assertEqual((self.root / ".release-image").read_text().strip(), f"algo:{self.old_ref}")
        self.assertEqual(self.active_units, before_timers)
        self.assertTrue((self.root / "instances" / "live.HALT").exists())

    def test_image_mismatch_fails_before_pausing_timers(self):
        self.expected_id = "sha256:" + "b" * 64
        before_timers = self.active_units.copy()

        with self.assertRaisesRegex(update.DeployError, "image ID does not match"):
            update.deploy(self.new_ref, "sha256:" + "a" * 64)

        self.assertEqual(self.current_ref, self.old_ref)
        self.assertEqual(self.active_units, before_timers)
        self.assertFalse(any(args[:2] == ("sudo", "-n") for args in self.actions))

    def test_backup_failure_resumes_timers_without_switching_code(self):
        self.stack.enter_context(mock.patch.object(update, "backup", side_effect=update.DeployError("backup failed")))
        before_timers = self.active_units.copy()

        with self.assertRaisesRegex(update.DeployError, "Previous release restored"):
            update.deploy(self.new_ref, self.expected_id)

        self.assertEqual(self.current_ref, self.old_ref)
        self.assertEqual(self.active_units, before_timers)
        self.assertEqual((self.root / ".release-image").read_text().strip(), f"algo:{self.old_ref}")

    def test_stopped_stack_is_not_started_by_update(self):
        self.stack.enter_context(mock.patch.object(update, "compose", return_value="db"))

        with self.assertRaisesRegex(update.DeployError, "stopped services must stay stopped"):
            update.deploy(self.new_ref, self.expected_id)

        self.assertEqual(self.current_ref, self.old_ref)
        self.assertFalse(any(args[:2] == ("sudo", "-n") for args in self.actions))


if __name__ == "__main__":
    unittest.main()
