import io
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import rotator_ui
import shared_mode
from rotator_ui import Dashboard
from test_helpers import write_executable


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

    def handler(self, dashboard, method, path, headers=None, body=b""):
        handler_type = rotator_ui.make_handler(dashboard, "test-only-token")
        handler = handler_type.__new__(handler_type)
        handler.server = SimpleNamespace(server_port=56729)
        handler.headers = {"Host": "127.0.0.1:56729", "X-Rotator-Token": "test-only-token", **(headers or {})}
        handler.path = path
        handler.rfile = io.BytesIO(body)
        handler._send_json = Mock()
        getattr(handler, method)()
        return handler._send_json.call_args.args

    def test_api_errors_come_back_as_json_instead_of_dropped_connections(self):
        dashboard = Mock()
        dashboard.accounts.side_effect = rotator_ui.RotatorError("Claude Code command not found: claude")
        dashboard.shared_status.side_effect = PermissionError("cannot write activity")
        dashboard.task_status.side_effect = RuntimeError("Could not determine home directory.")
        for path, message in (("/api/accounts", "Claude Code command not found: claude"),
                              ("/api/shared", "cannot write activity"),
                              ("/api/task?project=~nobody/app", "Could not determine home directory.")):
            self.assertEqual(self.handler(dashboard, "do_GET", path), ({"error": message}, 400))
        dashboard.set_threshold.side_effect = TypeError("float() argument must be a number")
        for headers, body in (({"Content-Length": "abc"}, b""),
                              ({"Content-Length": "16"}, b'{"percent":null}')):
            value, status = self.handler(dashboard, "do_POST", "/api/threshold", headers, body)
            self.assertEqual(status, 400)
            self.assertIn("error", value)


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
        self.fake = write_executable(self.root / "claude", FAKE_CLAUDE)
        self.config = self.root / "rotator.json"
        self.config.write_text(json.dumps({
            "claude_command": str(self.fake), "permission_mode": "auto", "max_runs": 4,
            "accounts": [{"name": "account-1", "config_dir": "profile/account-1",
                          "threshold_percent": 95, "usage_command": None}],
        }))
        self.dashboard = Dashboard(self.config)
        self.addCleanup(self.dashboard.shutdown)

    def wait_for_exit(self):
        deadline = time.monotonic() + 30  # three interpreter start-ups; slow CI machines need the headroom
        while time.monotonic() < deadline:
            status = self.dashboard.task_status(str(self.project))
            if not status["busy"] and status["exit_code"] is not None:
                return status
            time.sleep(0.05)
        self.fail("Dashboard process did not exit")

    def test_add_account_rejects_profile_folder_already_in_use(self):
        data = json.loads(self.config.read_text())
        data["accounts"][0]["name"] = "work"
        self.config.write_text(json.dumps(data))
        with self.assertRaisesRegex(rotator_ui.RotatorError, "own config_dir"):
            self.dashboard.add_account("account-1", "", "")
        self.assertEqual([item["name"] for item in self.dashboard.accounts()], ["work"])

    def test_status_pass_that_overlaps_an_account_change_is_recomputed(self):
        real = rotator_ui.auth_status
        added = []
        def status_then_add(binary, account):
            if not added:
                added.append(True)
                self.dashboard.add_account("account-2", "", "")
            return real(binary, account)
        with patch.object(rotator_ui, "auth_status", side_effect=status_then_add), \
             patch.object(shared_mode, "STATE_FILE", self.root / "missing-state.json"):
            self.assertEqual([item["name"] for item in self.dashboard.accounts()], ["account-1", "account-2"])
            self.assertEqual(self.dashboard.account_info("account-2")["name"], "account-2")

    def test_link_code_opens_the_dashboard_once_and_only_from_this_computer(self):
        server = rotator_ui.ThreadingHTTPServer(("127.0.0.1", 0), rotator_ui.make_handler(self.dashboard, "test-token"))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(path, body, **headers):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", method="POST",
                                             data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", **headers})
            try:
                with opener.open(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())
        self.assertEqual(post("/api/link-code", {})[0], 403)  # asking for a code needs the token
        code = post("/api/link-code", {}, **{"X-Rotator-Token": "test-token"})[1]["code"]
        self.assertEqual(post("/api/redeem", {"code": code}, Origin="http://evil.example")[0], 403)
        self.assertEqual(post("/api/redeem", {"code": code}), (200, {"token": "test-token"}))
        self.assertEqual(post("/api/redeem", {"code": code})[0], 403)  # single use

    def test_cancelled_sign_in_is_not_reported_as_a_failure(self):
        self.dashboard._start([sys.executable, "-c", "import time; time.sleep(30)"], "login:account-1")
        self.dashboard.stop()
        deadline = time.monotonic() + 30
        while "Sign-in cancelled" not in (status := self.dashboard.task_status(None))["messages"] or status["busy"]:
            self.assertLess(time.monotonic(), deadline, "the cancelled sign-in did not finish")
            time.sleep(0.05)
        self.assertIsNone(status["exit_code"])  # so the switching panel shows no failed change

    def test_shutdown_lets_shared_login_change_finish(self):
        self.dashboard._start([sys.executable, "-c", "import time; time.sleep(0.5)"], "shared-switch")
        self.dashboard.shutdown()
        self.assertEqual(self.dashboard.process.wait(), 0)

    def test_dashboard_link_is_private_and_updates_on_restart(self):
        path = rotator_ui.save_dashboard_url(self.config, "http://127.0.0.1:56729/#token=first")
        self.assertEqual(path.read_text().strip(), "http://127.0.0.1:56729/#token=first")
        if sys.platform != "win32":
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
        with patch.object(shared_mode, "STATE_FILE", state_file):
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
        with patch.object(shared_mode, "STATE_FILE", state_file), \
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
        with patch.object(shared_mode, "STATE_FILE", self.root / "missing-state.json"), \
             patch.object(rotator_ui, "shared_lock"):
            self.dashboard.remove_account("account-2", delete=True)
        self.assertEqual([item["name"] for item in self.dashboard.accounts()], ["account-1"])
        self.assertEqual((profile / "notes.txt").read_text(), "keep local history")

    def test_current_account_cannot_logout_while_switching(self):
        state_file = self.root / "shared-state.json"
        state_file.write_text(json.dumps({"enabled": True, "current": "account-1",
                                          "identities": {"account-1": "one@example.com|org"}}))
        with patch.object(shared_mode, "STATE_FILE", state_file), \
             patch.object(rotator_ui, "shared_lock"):
            with self.assertRaisesRegex(rotator_ui.RotatorError, "Switch the shared login"):
                self.dashboard.remove_account("account-1")
        self.assertTrue(self.dashboard.accounts()[0]["signed_in"])


if __name__ == "__main__":
    unittest.main()
