import io
import base64
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import shared_mode
from rotator import Account, Config, RotatorError

REAL_KEYCHAIN_WRITE = shared_mode.keychain_write


class SharedModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "rotator.json"
        self.accounts = [Account(f"account-{n}", f"Seat {n}", "same@example.com",
                                 self.root / f"profile-{n}", 95, None) for n in (1, 2)]
        self.config = Config("claude", "auto", 5, self.accounts)
        self.current_identity = "same@example.com|org-1"
        self.keychain = {}
        for n, account in enumerate(self.accounts, 1):
            self.keychain[shared_mode.keychain_service(account.config_dir)] = self.raw(n, 1)
        self.keychain[shared_mode.keychain_service(None)] = self.raw(1, 1)
        self.patchers = [
            patch.object(shared_mode, "STATE_DIR", self.root / "state"),
            patch.object(shared_mode, "STATE_FILE", self.root / "state" / "state.json"),
            patch.object(shared_mode, "require_macos"),
            patch.object(shared_mode, "claude_binary", return_value="/fake/claude"),
            patch.object(shared_mode, "keychain_read", side_effect=self.keychain_read),
            patch.object(shared_mode, "vault_read", side_effect=self.keychain_read),
            patch.object(shared_mode, "keychain_write", side_effect=self.keychain_write),
            patch.object(shared_mode, "auth_identity", side_effect=self.auth_identity),
            patch.object(shared_mode.subprocess, "run", side_effect=self.run_claude),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def raw(account, version):
        return json.dumps({"claudeAiOauth": {"refreshToken": f"refresh-{account}-{version}",
                                             "scopes": ["user:profile", "user:inference"]}})

    def keychain_read(self, service):
        if service not in self.keychain:
            raise RotatorError("Missing test credential")
        return self.keychain[service]

    def keychain_write(self, service, value):
        self.keychain[service] = value

    def auth_identity(self, binary, env):
        if "CLAUDE_CONFIG_DIR" in env:
            account = next(a for a in self.accounts if str(a.config_dir) == env["CLAUDE_CONFIG_DIR"])
            return f"{account.email}|org-{account.name[-1]}"
        return self.current_identity

    def run_claude(self, argv, **kwargs):
        self.assertEqual(argv, ["/fake/claude", "auth", "login", "--claudeai"])
        token = kwargs["env"]["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"]
        account = int(token.split("-")[1])
        version = int(token.split("-")[2]) + 1
        self.current_identity = f"same@example.com|org-{account}"
        self.keychain[shared_mode.keychain_service(None)] = self.raw(account, version)
        return SimpleNamespace(returncode=0)

    def test_import_switch_and_rate_limit_hook(self):
        state = shared_mode.setup(self.config, self.config_path)
        self.assertEqual(state["current"], "account-1")
        self.assertFalse(state["enabled"])
        state["enabled"] = True
        shared_mode.state_save(state)
        with patch("sys.stdin", io.StringIO('{"error":"rate_limit"}')):
            shared_mode.hook(self.config, self.config_path)
        updated = shared_mode.state_read()
        self.assertEqual(updated["current"], "account-2")
        self.assertEqual(self.current_identity, "same@example.com|org-2")
        self.assertEqual(updated["history"][-1]["reason"], "rate_limit")
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        self.assertIn("refresh-2-2", self.keychain[vault])
        with patch("sys.stdin", io.StringIO('{"error":"rate_limit"}')):
            shared_mode.hook(self.config, self.config_path)
        self.assertEqual(shared_mode.state_read()["current"], "account-2")

    def test_limit_fallback_can_be_disabled_without_stopping_session_checks(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["enabled"] = True
        state["fallback_on_limit"] = False
        shared_mode.state_save(state)
        with patch("sys.stdin", io.StringIO('{"error":"rate_limit"}')):
            shared_mode.hook(self.config, self.config_path)
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_session_threshold_switches_current_account(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["enabled"] = True
        shared_mode.state_save(state)
        with patch.object(shared_mode, "read_session_usage", return_value=(96.0, None)), \
             patch.object(shared_mode, "load_config", return_value=self.config), \
             patch.object(shared_mode.time, "sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                shared_mode.watch(self.config, self.config_path)
        updated = shared_mode.state_read()
        self.assertEqual(updated["current"], "account-2")
        self.assertEqual(updated["history"][-1]["reason"], "96% usage")

    def test_older_vault_migrates_to_signed_security_items(self):
        state = shared_mode.setup(self.config, self.config_path)
        state.pop("vault_version")
        for account in self.accounts:
            raw = self.keychain.pop(shared_mode.vault_service(self.config_path, account))
            self.keychain[shared_mode.old_vault_service(self.config_path, account)] = raw
        shared_mode.state_save(state)
        with patch.object(shared_mode, "old_vault_read", side_effect=self.keychain_read), \
             patch.object(shared_mode, "vault_delete") as delete_old:
            migrated = shared_mode.migrate_vault(self.config, self.config_path, shared_mode.state_read())
        self.assertEqual(migrated["vault_version"], 3)
        self.assertEqual(delete_old.call_count, 2)
        self.assertTrue(all(shared_mode.vault_service(self.config_path, account) in self.keychain
                            for account in self.accounts))

    def test_failed_identity_verification_keeps_state(self):
        shared_mode.setup(self.config, self.config_path)
        self.current_identity = "unexpected@example.com|org-1"
        with self.assertRaises(RotatorError):
            shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[1], "manual")
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_hook_install_preserves_existing_hooks_and_is_idempotent(self):
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"permissions": {"defaultMode": "default"},
                                        "hooks": {"StopFailure": [{"matcher": "server_error", "hooks": [
                                            {"type": "command", "command": "echo server"}]}]}}))
        shared_mode.install_hook(self.config_path, settings)
        shared_mode.install_hook(self.config_path, settings)
        value = json.loads(settings.read_text())
        self.assertEqual(value["permissions"]["defaultMode"], "default")
        self.assertEqual(len(value["hooks"]["StopFailure"]), 2)
        shared_mode.remove_hook(settings)
        value = json.loads(settings.read_text())
        self.assertEqual(len(value["hooks"]["StopFailure"]), 1)
        self.assertEqual(value["hooks"]["StopFailure"][0]["matcher"], "server_error")

    def test_background_monitor_installs_without_shell_or_secrets(self):
        path = self.root / "LaunchAgents" / "switchboard.plist"
        with patch.object(shared_mode, "agent_path", return_value=("switchboard.test", path)), \
             patch.object(shared_mode.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            shared_mode.install_agent(self.config_path)
        value = plistlib.loads(path.read_bytes())
        self.assertEqual(value["Label"], "switchboard.test")
        self.assertEqual(value["ProgramArguments"][-1], "watch")
        self.assertNotIn("refreshToken", path.read_text())

    def test_keychain_write_passes_secret_only_on_stdin(self):
        with patch.object(shared_mode.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"", stderr=b"")) as run:
            REAL_KEYCHAIN_WRITE("Switchboard-test", "private-credential")
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/security", "add-generic-password"])
        self.assertNotIn("private-credential", str(args))
        encoded = base64.b64encode(b"private-credential")
        self.assertEqual(run.call_args.kwargs["input"], encoded + b"\n" + encoded + b"\n")


if __name__ == "__main__":
    unittest.main()
