import json
import stat
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import rotator_ui
from rotator_ui import Dashboard


class AuthorizationTests(unittest.TestCase):
    def test_private_api_rejects_wrong_token_host_or_origin(self):
        handler_type = rotator_ui.make_handler(None, "test-only-token")
        for overrides in ({"X-Rotator-Token": "wrong"}, {"Host": "evil.example"},
                          {"Origin": "https://evil.example"}):
            handler = handler_type.__new__(handler_type)
            handler.server = SimpleNamespace(server_port=56729)
            handler.headers = {"Host": "127.0.0.1:56729", "X-Rotator-Token": "test-only-token", **overrides}
            handler._send_json = Mock()
            self.assertFalse(handler._authorized())
            self.assertEqual(handler._send_json.call_args.args[1], 403)

    def test_private_api_accepts_valid_local_request(self):
        handler_type = rotator_ui.make_handler(None, "test-only-token")
        handler = handler_type.__new__(handler_type)
        handler.server = SimpleNamespace(server_port=56729)
        handler.headers = {"Host": "127.0.0.1:56729", "Origin": "http://127.0.0.1:56729",
                           "X-Rotator-Token": "test-only-token"}
        self.assertTrue(handler._authorized())


FAKE_CLAUDE = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
profile = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/tmp/claude-default-test"))
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": not (profile / ".signed_out").exists(), "authMethod": "claude.ai"}))
elif sys.argv[1:3] == ["auth", "login"]:
    (profile / ".signed_out").unlink(missing_ok=True)
    print("Login successful")
elif sys.argv[1:3] == ["auth", "logout"]:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".signed_out").write_text("signed out")
    print("Logout successful")
elif "-p" in sys.argv:
    print(json.dumps({"is_error": False, "session_id": "test-session", "structured_output": {
        "status": "complete", "summary": "Finished test task", "next_steps": ""}}))
else:
    sys.exit(1)
'''


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.fake = self.root / "claude"
        self.fake.write_text(FAKE_CLAUDE)
        self.fake.chmod(0o755)
        self.config = self.root / "rotator.json"
        self.config.write_text(json.dumps({
            "claude_command": str(self.fake), "permission_mode": "auto", "max_runs": 4,
            "accounts": [{"name": "account-1", "config_dir": "profile/account-1",
                          "threshold_percent": 95, "usage_command": None}],
        }))
        self.dashboard = Dashboard(self.config)
        self.addCleanup(self.dashboard.shutdown)

    def wait_for_exit(self):
        for _ in range(100):
            status = self.dashboard.task_status(str(self.project))
            if not status["busy"] and status["exit_code"] is not None:
                return status
            time.sleep(0.02)
        self.fail("Dashboard process did not exit")

    def test_dashboard_link_is_private_and_updates_on_restart(self):
        path = rotator_ui.save_dashboard_url(self.config, "http://127.0.0.1:56729/#token=first")
        self.assertEqual(path.read_text().strip(), "http://127.0.0.1:56729/#token=first")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        rotator_ui.save_dashboard_url(self.config, "http://127.0.0.1:56729/#token=second")
        self.assertEqual(path.read_text().strip(), "http://127.0.0.1:56729/#token=second")

    def test_account_management_and_task_progress(self):
        self.dashboard.add_account("account-2", "two@example.com", "Research seat")
        self.dashboard.set_display_name("account-1", "Primary seat")
        self.dashboard.set_threshold("account-2", 90)
        accounts = self.dashboard.accounts()
        self.assertEqual([item["name"] for item in accounts], ["account-1", "account-2"])
        self.assertEqual([item["display_name"] for item in accounts], ["Primary seat", "Research seat"])
        self.assertTrue(all(item["signed_in"] for item in accounts))
        self.assertEqual(accounts[1]["threshold_percent"], 90)
        self.assertEqual(json.loads(self.config.read_text())["accounts"][0]["config_dir"], "profile/account-1")

        self.dashboard.start_task(str(self.project), "Finish a task")
        status = self.wait_for_exit()
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["saved"]["status"], "complete")
        self.assertEqual(status["saved"]["runs"][0]["account"], "account-1")
        self.assertIn("Task complete", "\n".join(status["messages"]))

    def test_login_uses_profile_and_finishes(self):
        self.dashboard.login("account-1")
        status = self.wait_for_exit()
        self.assertEqual(status["exit_code"], 0)
        self.assertTrue((self.root / "profile" / "account-1").is_dir())

    def test_shared_mode_status_and_action(self):
        state_file = self.root / "shared-state.json"
        state_file.write_text(json.dumps({"enabled": True, "current": "account-1",
                                          "identities": {"account-1": "one@example.com|org"},
                                          "history": [], "last_error": None}))
        with patch.object(rotator_ui, "STATE_FILE", state_file):
            status = self.dashboard.shared_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["imported_count"], 1)
        self.assertEqual(status["current"], "account-1")
        self.assertTrue(status["accounts"][0]["prepared"])
        with patch.object(self.dashboard, "_start") as start:
            self.dashboard.shared_action("switch", "account-1")
        self.assertEqual(start.call_args.args[0][-2:], ["switch", "account-1"])
        self.assertEqual(start.call_args.args[1], "shared-switch")

    def test_account_info_and_logout_remove_prepared_login(self):
        self.dashboard.add_account("account-2", "", "Second seat")
        self.dashboard.add_account("account-3", "", "Third seat")
        state_file = self.root / "shared-state.json"
        state_file.write_text(json.dumps({"enabled": True, "current": "account-1",
                                          "identities": {"account-1": "one@example.com|org1",
                                                         "account-2": "two@example.com|org2",
                                                         "account-3": "three@example.com|org3"},
                                          "limited": {}, "history": [{"from": "account-3", "to": "account-1", "reason": "6% usage", "at": time.time()}],
                                          "login_errors": {"account-3": "Saved login rejected"}, "last_error": None}))
        with patch.object(rotator_ui, "STATE_FILE", state_file), \
             patch.object(rotator_ui, "shared_lock"), \
             patch.object(rotator_ui, "vault_delete") as delete_vault:
            info = self.dashboard.account_info("account-3")
            self.assertEqual(info["name"], "account-3")
            self.assertEqual(info["organization_id"], "org3")
            self.assertTrue(info["connected_since"])
            self.assertIsNone(info["last_login_at"])
            self.assertEqual(info["login_error"], "Saved login rejected")
            self.assertTrue(any("session threshold" in event["message"] for event in info["events"]))
            self.dashboard.remove_account("account-3")
            self.assertFalse(self.dashboard.account_info("account-3")["signed_in"])
            self.assertIsNotNone(self.dashboard.account_info("account-3")["last_logout_at"])
            self.assertNotIn("account-3", json.loads(state_file.read_text())["identities"])
            self.assertNotIn("account-3", json.loads(state_file.read_text())["login_errors"])
            self.assertEqual(len(json.loads(self.config.read_text())["accounts"]), 3)
            self.assertEqual(delete_vault.call_count, 3)

    def test_delete_account_keeps_profile_folder(self):
        self.dashboard.add_account("account-2", "", "Second seat")
        profile = self.root / "profile" / "account-2"
        profile.mkdir(parents=True)
        (profile / "notes.txt").write_text("keep local history")
        with patch.object(rotator_ui, "STATE_FILE", self.root / "missing-state.json"), \
             patch.object(rotator_ui, "shared_lock"):
            self.dashboard.remove_account("account-2", delete=True)
        self.assertEqual([item["name"] for item in self.dashboard.accounts()], ["account-1"])
        self.assertEqual((profile / "notes.txt").read_text(), "keep local history")

    def test_current_account_cannot_logout_while_switching(self):
        state_file = self.root / "shared-state.json"
        state_file.write_text(json.dumps({"enabled": True, "current": "account-1",
                                          "identities": {"account-1": "one@example.com|org"}}))
        with patch.object(rotator_ui, "STATE_FILE", state_file), \
             patch.object(rotator_ui, "shared_lock"):
            with self.assertRaisesRegex(rotator_ui.RotatorError, "Switch the shared login"):
                self.dashboard.remove_account("account-1")
        self.assertTrue(self.dashboard.accounts()[0]["signed_in"])


if __name__ == "__main__":
    unittest.main()
