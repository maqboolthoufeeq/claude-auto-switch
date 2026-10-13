import io
import base64
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import shared_mode
from rotator import Account, Config, RotatorError

REAL_KEYCHAIN_ITEM_WRITE = shared_mode._keychain_item_write


class VaultStorageTests(unittest.TestCase):
    def setUp(self):
        self.items = {}
        self.service = "Switchboard-v4-storage-test"
        def read(name):
            if name not in self.items:
                raise RotatorError("Missing test item")
            return self.items[name]
        for patcher in (patch.object(shared_mode, "_keychain_item_read", side_effect=read),
                        patch.object(shared_mode, "_keychain_item_write", side_effect=lambda name,value: self.items.__setitem__(name,value)),
                        patch.object(shared_mode, "_keychain_item_delete", side_effect=lambda name: self.items.pop(name,None))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_long_credentials_roundtrip_update_and_delete_all_chunks(self):
        raw = json.dumps({"credential": "long-value-" * 2000})
        shared_mode.keychain_write(self.service, raw)
        self.assertEqual(shared_mode.vault_read(self.service), raw)
        self.assertTrue(all(len(value) <= 512 for name,value in self.items.items() if name != self.service))
        shared_mode.keychain_write(self.service, "updated value")
        self.assertEqual(shared_mode.vault_read(self.service), "updated value")
        self.assertEqual(len(self.items), 2)
        shared_mode.vault_delete(self.service)
        self.assertEqual(self.items, {})

    def test_interrupted_write_preserves_previous_credential(self):
        shared_mode.keychain_write(self.service, "previous value")
        original = dict(self.items)
        def fail_chunk(name,value):
            if name.endswith("-1"):
                raise RotatorError("Test write interrupted")
            self.items[name] = value
        with patch.object(shared_mode,"_keychain_item_write",side_effect=fail_chunk):
            with self.assertRaises(RotatorError):
                shared_mode.keychain_write(self.service,"x" * 2000)
        self.assertEqual(shared_mode.vault_read(self.service), "previous value")
        self.assertEqual(self.items, original)

    def test_corrupt_chunk_is_rejected(self):
        shared_mode.keychain_write(self.service,"x" * 2000)
        manifest=shared_mode._vault_manifest(self.service)
        name=f"{self.service}-{manifest['g']}-0"
        self.items[name]=b"Y" + self.items[name][1:]
        with self.assertRaises(RotatorError):
            shared_mode.vault_read(self.service)

    def test_failed_manifest_verification_restores_previous_credential(self):
        shared_mode.keychain_write(self.service, "previous value")
        original = dict(self.items)
        failed = False
        def fail_root(name, value):
            nonlocal failed
            self.items[name] = value
            if name == self.service and not failed:
                failed = True
                raise RotatorError("Test verification interrupted")
        with patch.object(shared_mode, "_keychain_item_write", side_effect=fail_root):
            with self.assertRaises(RotatorError):
                shared_mode.keychain_write(self.service, "new value" * 1000)
        self.assertEqual(shared_mode.vault_read(self.service), "previous value")
        self.assertEqual(self.items, original)


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
            patch.object(shared_mode, "claude_credential", side_effect=lambda directory: self.keychain_read(shared_mode.keychain_service(directory))),
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

    def test_no_available_account_records_threshold_and_error(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["enabled"] = True
        state["limited"] = {"account-2": shared_mode.time.time()}
        shared_mode.state_save(state)
        with patch.object(shared_mode, "read_session_usage", return_value=(96.0, None)), \
             patch.object(shared_mode, "load_config", return_value=self.config), \
             patch.object(shared_mode.time, "sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                shared_mode.watch(self.config, self.config_path)
        updated = shared_mode.state_read()
        self.assertEqual(updated["current"], "account-1")
        self.assertIn("account-1", updated["limited"])
        self.assertIn("No prepared account", updated["last_error"])

    def test_older_vault_migrates_to_signed_security_items(self):
        state = shared_mode.setup(self.config, self.config_path)
        state.pop("vault_version")
        for account in self.accounts:
            raw = self.keychain.pop(shared_mode.vault_service(self.config_path, account))
            self.keychain[shared_mode.old_vault_service(self.config_path, account)] = raw
        shared_mode.state_save(state)
        with patch.object(shared_mode, "old_vault_read", side_effect=self.keychain_read), \
             patch.object(shared_mode, "_keychain_item_read", side_effect=RotatorError("Missing old item")), \
             patch.object(shared_mode, "vault_delete") as delete_old:
            migrated = shared_mode.migrate_vault(self.config, self.config_path, shared_mode.state_read())
        self.assertEqual(migrated["vault_version"], 4)
        self.assertEqual(delete_old.call_count, 4)
        self.assertTrue(all(shared_mode.vault_service(self.config_path, account) in self.keychain
                            for account in self.accounts))

    def test_failed_identity_verification_keeps_state(self):
        shared_mode.setup(self.config, self.config_path)
        self.current_identity = "unexpected@example.com|org-1"
        with self.assertRaises(RotatorError):
            shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[1], "manual")
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_failed_target_credential_save_restores_shared_login(self):
        state = shared_mode.setup(self.config, self.config_path)
        def fail_target(service, value):
            if service == shared_mode.vault_service(self.config_path, self.accounts[1]):
                raise RotatorError("Test storage interrupted")
            self.keychain_write(service, value)
        with patch.object(shared_mode, "keychain_write", side_effect=fail_target):
            with self.assertRaises(RotatorError):
                shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertEqual(self.current_identity, "same@example.com|org-1")
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_rejected_login_is_skipped_until_prepared_again(self):
        state = shared_mode.setup(self.config, self.config_path)
        with patch.object(shared_mode, "claude_login", return_value=False):
            with self.assertRaises(RotatorError):
                shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertIn("account-2", state["login_errors"])
        self.assertIsNone(shared_mode.next_account(self.config, state))
        shared_mode.state_save(state)
        repaired = shared_mode.setup(self.config, self.config_path)
        self.assertEqual(shared_mode.next_account(self.config, repaired), self.accounts[1])

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
             patch.object(shared_mode.sys, "platform", "darwin"), \
             patch.object(shared_mode.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            shared_mode.install_agent(self.config_path)
        value = plistlib.loads(path.read_bytes())
        self.assertEqual(value["Label"], "switchboard.test")
        self.assertEqual(value["ProgramArguments"][-1], "watch")
        self.assertEqual(value["EnvironmentVariables"], {"PATH": os.environ.get("PATH", os.defpath)})
        self.assertNotIn("refreshToken", path.read_text())

    def test_keychain_write_passes_secret_only_on_stdin(self):
        encoded = base64.b64encode(b"private-credential")
        with patch.object(shared_mode.sys, "platform", "darwin"), \
             patch.object(shared_mode.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=encoded+b"\n", stderr=b"")) as run:
            REAL_KEYCHAIN_ITEM_WRITE("Switchboard-test", encoded)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/security", "find-generic-password"])
        write_call=run.call_args_list[0]
        self.assertEqual(write_call.args[0],["/usr/bin/security","-i"])
        self.assertNotIn("private-credential", str(args))
        self.assertIn(encoded,write_call.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
