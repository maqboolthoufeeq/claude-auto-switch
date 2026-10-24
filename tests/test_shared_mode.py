import io
import base64
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import platform_runtime
import shared_mode
from rotator import Account, Config, RotatorError

REAL_KEYCHAIN_ITEM_WRITE = shared_mode._keychain_item_write
REAL_POPEN = subprocess.Popen


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
        self.addCleanup(os.chdir, os.getcwd())  # main() changes into the data folder; leave it before it is deleted
        self.root = Path(self.temp.name)
        self.config_path = self.root / "rotator.json"
        self.accounts = [Account(f"account-{n}", f"Seat {n}", "same@example.com",
                                 self.root / f"profile-{n}", 95, None) for n in (1, 2)]
        self.config = Config("claude", "auto", 5, self.accounts)
        self.current_identity = "same@example.com|org-1"
        self.logins = []
        self.keychain = {}
        for n, account in enumerate(self.accounts, 1):
            self.keychain[shared_mode.keychain_service(account.config_dir)] = self.raw(n, 1)
        self.keychain[shared_mode.keychain_service(None)] = self.raw(1, 1)
        self.patchers = [
            patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}),
            patch.object(shared_mode, "STATE_DIR", self.root / ".shared-rotator"),
            patch.object(shared_mode, "STATE_FILE", self.root / ".shared-rotator" / "state.json"),
            patch.object(shared_mode, "require_macos"),
            patch.object(shared_mode, "private_directory", side_effect=lambda path: path.mkdir(parents=True, exist_ok=True)),
            patch.object(shared_mode, "claude_binary", return_value="/fake/claude"),
            patch.object(shared_mode, "keychain_read", side_effect=self.keychain_read),
            patch.object(shared_mode, "claude_credential", side_effect=lambda directory: self.keychain_read(shared_mode.keychain_service(directory))),
            patch.object(shared_mode, "vault_read", side_effect=self.keychain_read),
            patch.object(shared_mode, "keychain_write", side_effect=self.keychain_write),
            patch.object(shared_mode, "auth_identity", side_effect=self.auth_identity),
            patch.object(shared_mode.subprocess, "Popen", side_effect=self.run_claude),
            patch.object(shared_mode.subprocess, "run", side_effect=AssertionError("unexpected subprocess.run")),
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
        if self.current_identity is None:
            raise RotatorError(shared_mode.NOT_SIGNED_IN)
        return self.current_identity

    def run_claude(self, argv, **kwargs):
        self.assertEqual(argv, ["/fake/claude", "auth", "login", "--claudeai"])
        token = kwargs["env"]["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"]
        self.logins.append(token)
        account = int(token.split("-")[1])
        version = int(token.split("-")[2]) + 1
        self.current_identity = f"same@example.com|org-{account}"
        self.keychain[shared_mode.keychain_service(None)] = self.raw(account, version)
        return SimpleNamespace(returncode=0)

    @staticmethod
    def darwin():
        return SimpleNamespace(platform="darwin", executable=sys.executable)

    def limit(self):
        with patch.object(shared_mode, "load_config", return_value=self.config):
            shared_mode.limit(self.config_path)

    def enabled_setup(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["enabled"] = True
        shared_mode.state_save(state)
        return state

    def test_import_switch_and_rate_limit_report(self):
        state = shared_mode.setup(self.config, self.config_path)
        self.assertEqual(state["current"], "account-1")
        self.assertFalse(state["enabled"])
        state["enabled"] = True
        shared_mode.state_save(state)
        self.limit()
        updated = shared_mode.state_read()
        self.assertEqual(updated["current"], "account-2")
        self.assertEqual(self.current_identity, "same@example.com|org-2")
        self.assertEqual(updated["history"][-1]["reason"], "rate_limit")
        self.assertIn("account-1", updated["limited"])
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        self.assertIn("refresh-2-2", self.keychain[vault])

    def test_hook_hands_rate_limits_to_a_detached_switch_and_ignores_other_errors(self):
        # Claude kills a hook's process tree when its session exits; the switch must not be in that tree.
        with patch.object(shared_mode.subprocess, "Popen") as spawn:
            for event in ('{"error":"overloaded"}', "not json", '["rate_limit"]'):
                with patch("sys.stdin", io.StringIO(event)):
                    shared_mode.hook(self.config_path)
            spawn.assert_not_called()
            with patch("sys.stdin", io.StringIO('{"error":"rate_limit","error_details":"429"}')):
                shared_mode.hook(self.config_path)
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[-3:], ["--config", str(self.config_path), "limit"])
        self.assertEqual(spawn.call_args.kwargs.get("stdin"), subprocess.DEVNULL)
        if sys.platform != "win32":
            self.assertTrue(spawn.call_args.kwargs.get("start_new_session"))

    def test_late_rate_limit_report_does_not_blame_the_new_account(self):
        self.enabled_setup()
        self.limit()
        state = shared_mode.state_read()
        state["history"][-1]["at"] -= 60  # an agent still on account-1's cached token fails a minute later
        shared_mode.state_save(state)
        self.limit()
        updated = shared_mode.state_read()
        self.assertEqual(updated["current"], "account-2")
        self.assertNotIn("account-2", updated["limited"])
        state = shared_mode.state_read()
        state["history"][-1]["at"] -= 60  # well past the grace window, the report is about account-2
        state["limited"]["account-1"] -= shared_mode.COOLDOWN_SECONDS
        shared_mode.state_save(state)
        self.limit()
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_limit_fallback_can_be_disabled_without_stopping_session_checks(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["enabled"] = True
        state["fallback_on_limit"] = False
        shared_mode.state_save(state)
        self.limit()
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_session_threshold_switches_current_account(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["enabled"] = True
        shared_mode.state_save(state)
        with patch.object(shared_mode, "read_session_usage", return_value=(96.0, None, None)), \
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
        with patch.object(shared_mode, "read_session_usage", return_value=(96.0, None, None)), \
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

    def test_landed_login_stays_when_its_vault_copy_cannot_be_saved(self):
        # Rolling back would throw away the only copy of the target's new refresh token.
        state = shared_mode.setup(self.config, self.config_path)
        def fail_target(service, value):
            if service == shared_mode.vault_service(self.config_path, self.accounts[1]):
                raise RotatorError("Test storage interrupted")
            self.keychain_write(service, value)
        with patch.object(shared_mode, "keychain_write", side_effect=fail_target):
            shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertEqual(self.current_identity, "same@example.com|org-2")
        self.assertEqual(shared_mode.state_read()["current"], "account-2")
        self.assertEqual(self.logins, ["refresh-2-1"])  # no rollback login
        shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[0], "manual")
        self.assertIn("refresh-2-2", self.keychain[shared_mode.vault_service(self.config_path, self.accounts[1])])

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
             patch.object(shared_mode, "sys", self.darwin()), \
             patch.object(shared_mode.os, "getuid", return_value=1000, create=True), \
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


    def test_monitor_survives_a_bad_tick_and_records_it(self):
        # An exception escaping the loop makes launchd/systemd restart the monitor in a tight loop.
        self.enabled_setup()
        with patch.object(shared_mode, "load_config", side_effect=RotatorError("Cannot read config: typo")), \
             patch.object(shared_mode.time, "sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                shared_mode.watch(None, self.config_path)
        self.assertEqual(shared_mode.state_read()["last_error"], "Cannot read config: typo")

    def test_monitor_stop_waits_for_a_running_usage_read(self):
        # Stopping the monitor mid-read would kill its Claude, possibly in the middle of a token refresh.
        self.enabled_setup()
        plist = self.root / "agent.plist"
        plist.write_text("")
        events = []
        stopper = threading.Thread(target=shared_mode.remove_agent, args=(self.config_path,))
        def read(*args):
            stopper.start()
            stopper.join(0.5)  # an unguarded stop would land now, while Claude is open
            events.append("read done")
            return 10.0, None, None
        with patch.object(shared_mode, "read_session_usage", side_effect=read), \
             patch.object(shared_mode, "load_config", return_value=self.config), \
             patch.object(shared_mode, "agent_path", return_value=("switchboard.test", plist)), \
             patch.object(shared_mode.os, "getuid", return_value=1000, create=True), \
             patch.object(shared_mode.subprocess, "run", side_effect=lambda argv, **kwargs: events.append("stop")), \
             patch.object(shared_mode.background_monitor, "remove", side_effect=lambda *args: events.append("stop")):
            shared_mode.check_usage(self.config_path)
            stopper.join(5)
        self.assertEqual(events, ["read done", "stop"])

    def test_monitor_follows_a_login_changed_to_another_prepared_account(self):
        self.enabled_setup()
        self.current_identity = "same@example.com|org-2"  # someone ran /login with account-2
        with patch.object(shared_mode, "read_session_usage", return_value=(50.0, None, None)), \
             patch.object(shared_mode, "load_config", return_value=self.config):
            shared_mode.check_usage(self.config_path)
            state = shared_mode.state_read()
            self.assertEqual(state["current"], "account-2")
            self.assertEqual(state["history"][-1]["reason"], "signed in outside Switchboard")
            self.current_identity = "stranger@example.com|org-9"  # not a prepared account: stop and say so
            shared_mode.check_usage(self.config_path)
        state = shared_mode.state_read()
        self.assertEqual(state["current"], "account-2")
        self.assertIn("changed outside Switchboard", state["session_usage"]["error"])

    def test_switch_follows_a_login_changed_outside_switchboard(self):
        state = shared_mode.setup(self.config, self.config_path)
        self.current_identity = "same@example.com|org-2"
        shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertEqual(shared_mode.state_read()["current"], "account-2")
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        self.assertEqual(self.keychain[vault], self.keychain[shared_mode.keychain_service(None)])

    def test_threshold_rest_ends_at_the_session_reset_shown_by_usage(self):
        self.enabled_setup()
        soon = time.time() + 600
        with patch.object(shared_mode, "read_session_usage", return_value=(96.0, None, soon)), \
             patch.object(shared_mode, "load_config", return_value=self.config):
            shared_mode.check_usage(self.config_path)
        state = shared_mode.state_read()
        self.assertEqual(state["current"], "account-2")
        self.assertEqual(state["resets"]["account-1"], soon)
        self.assertTrue(shared_mode.resting(state, "account-1", soon - 1))
        self.assertFalse(shared_mode.resting(state, "account-1", soon + 1))
        shared_mode.mark_limited(state, "account-1")  # a limit report without a reset time rests the full window
        self.assertTrue(shared_mode.resting(state, "account-1", soon + 1))

    @unittest.skipIf(sys.platform == "win32", "POSIX sessions and signals")
    def test_ctrl_c_lets_a_running_login_finish_in_its_own_session(self):
        # A login cut short can leave the shared login empty, signing every Claude session out.
        done, fake = self.root / "done", self.root / "claude"
        fake.write_text(f"#!{sys.executable}\nimport os, time\ntime.sleep(2)\n"
                        f"open({str(done)!r}, 'w').write(str(os.getsid(0)))\n")
        fake.chmod(0o700)
        self.addCleanup(signal.signal, signal.SIGINT, signal.signal(signal.SIGINT, signal.default_int_handler))
        threading.Timer(0.3, signal.pthread_kill, (threading.main_thread().ident, signal.SIGINT)).start()
        with patch.object(shared_mode.subprocess, "Popen", REAL_POPEN):
            try:
                ok = shared_mode.claude_login(str(fake), self.raw(2, 1))
            except KeyboardInterrupt:
                self.fail("Ctrl+C cut a running Claude login short")
        self.assertTrue(ok)
        self.assertNotEqual(int(done.read_text()), os.getsid(0))

    def test_failed_switch_persists_login_error(self):
        shared_mode.setup(self.config, self.config_path)
        with patch.object(shared_mode, "claude_login", return_value=False):
            with self.assertRaises(RotatorError):
                shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[1], "manual")
        saved = shared_mode.state_read()
        self.assertIn("account-2", saved.get("login_errors", {}))
        self.assertIn("rejected", saved["last_error"] or "")

    def test_post_login_identity_mismatch_rolls_back_without_storing_the_wrong_login(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["identities"]["account-2"] = "same@example.com|org-7"
        with self.assertRaisesRegex(RotatorError, "verification failed"):
            shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        self.assertIn("refresh-2-1", self.keychain[vault])
        self.assertEqual(self.current_identity, "same@example.com|org-1")
        self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_unverified_login_keeps_the_rotated_token_for_the_target(self):
        # The login spent the saved refresh token; rolling back must not leave the vault with the spent one.
        state = shared_mode.setup(self.config, self.config_path)
        calls = []
        def flaky(binary, env):
            if "CLAUDE_CONFIG_DIR" not in env:
                calls.append(env)
                if len(calls) == 2:  # the check right after the login times out
                    raise RotatorError("Could not verify Claude login: timed out")
            return self.auth_identity(binary, env)
        with patch.object(shared_mode, "auth_identity", side_effect=flaky):
            shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        self.assertIn("refresh-2-2", self.keychain[vault])
        self.assertEqual(shared_mode.state_read()["current"], "account-2")  # verified on the second check
        self.assertEqual(self.logins, ["refresh-2-1"])

    def test_setup_rebuilds_damaged_state(self):
        shared_mode.STATE_DIR.mkdir(parents=True)
        for text in ("{ not json", "{}"):
            with self.subTest(text=text):
                shared_mode.STATE_FILE.write_text(text, encoding="utf-8")
                state = shared_mode.setup(self.config, self.config_path)
                self.assertEqual(list(state["identities"]), ["account-1", "account-2"])
                self.assertEqual(shared_mode.state_read()["current"], "account-1")

    def test_prepare_again_keeps_rotated_login_until_profile_signs_in_again(self):
        shared_mode.setup(self.config, self.config_path)
        shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[1], "manual")
        shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[0], "manual")
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        shared_mode.setup(self.config, self.config_path)
        self.assertIn("refresh-2-2", self.keychain[vault])
        self.keychain[shared_mode.keychain_service(self.accounts[1].config_dir)] = self.raw(2, 9)
        shared_mode.setup(self.config, self.config_path)
        self.assertIn("refresh-2-9", self.keychain[vault])

    def test_enable_without_current_account_points_to_switch(self):
        self.current_identity = "other@example.com|org-9"
        self.assertIsNone(shared_mode.setup(self.config, self.config_path)["current"])
        with patch.object(shared_mode, "load_config", return_value=self.config), \
             patch.object(shared_mode, "install_hook") as install_hook, \
             patch.object(shared_mode, "install_agent"), \
             patch("sys.argv", ["shared_mode.py", "--config", str(self.config_path), "enable"]):
            with self.assertRaisesRegex(RotatorError, r"switch <account>"):
                shared_mode.main()
        install_hook.assert_not_called()

    def test_ensure_monitor_refreshes_hook_and_monitor_when_switching_is_on(self):
        # After an upgrade moves the code, the hook command must follow the monitor to the new path.
        self.enabled_setup()
        with patch.object(shared_mode, "install_hook") as install_hook, \
             patch.object(shared_mode, "install_agent") as install_agent, \
             patch("sys.argv", ["shared_mode.py", "--config", str(self.config_path), "ensure-monitor"]):
            shared_mode.main()
        self.assertEqual(install_hook.call_args.args[0], self.config_path.resolve())
        install_agent.assert_called_once_with(self.config_path.resolve())

    def test_switching_is_not_installed_from_an_npx_cache(self):
        self.enabled_setup()
        cached = Path("/home/u/.npm/_npx/0a1b2c/node_modules/cswitch/shared_mode.py")
        with patch.object(shared_mode, "__file__", str(cached)), \
             patch.object(shared_mode, "install_hook"), \
             patch.object(shared_mode, "install_agent") as install_agent, \
             patch("sys.argv", ["shared_mode.py", "--config", str(self.config_path), "ensure-monitor"]):
            with self.assertRaisesRegex(RotatorError, "npm install -g cswitch"):
                shared_mode.main()
        install_agent.assert_not_called()

    def test_remove_hook_leaves_settings_without_switchboard_hook_untouched(self):
        settings = self.root / "settings.json"
        for value in ({"permissions": {}}, {"hooks": None}, {"hooks": {"PreToolUse": []}}):
            settings.write_text(json.dumps(value))
            shared_mode.remove_hook(settings)
            self.assertEqual(json.loads(settings.read_text()), value)


    def test_hook_runs_from_the_data_folder_not_the_claude_session_folder(self):
        # Windows finds claude.exe and powershell.exe in the current folder first; the package folder would be
        # pinned by the long-running monitor and block npm upgrades.
        session = self.root / "someone else's repo"
        session.mkdir()
        os.chdir(session)
        seen = []
        with patch.object(shared_mode.subprocess, "Popen", side_effect=lambda *a, **k: seen.append(Path.cwd())), \
             patch("sys.stdin", io.StringIO('{"error":"rate_limit"}')), \
             patch("sys.argv", ["shared_mode.py", "--config", str(self.config_path), "hook"]):
            shared_mode.main()
        self.assertEqual(seen, [self.config_path.resolve().parent])

    def test_enabling_a_monitor_retires_monitors_of_other_installs(self):
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        stale = agents / "com.local.switchboard.shared-login.0123456789ab.plist"
        stale.write_bytes(plistlib.dumps({"Label": stale.stem, "KeepAlive": True}))
        path = agents / "com.local.switchboard.shared-login.fedcba987654.plist"
        with patch.object(shared_mode, "agent_path", return_value=(path.stem, path)), \
             patch.object(shared_mode, "sys", self.darwin()), \
             patch.object(shared_mode.os, "getuid", return_value=1000, create=True), \
             patch.object(shared_mode.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            shared_mode.install_agent(self.config_path)
        run.assert_any_call(["launchctl", "bootout", "gui/1000", str(stale)],
                            capture_output=True, check=False, timeout=10)
        self.assertFalse(stale.exists())
        self.assertTrue(path.exists())

    def test_no_available_account_error_clears_once_usage_is_back_under_the_threshold(self):
        state = self.enabled_setup()
        state["limited"] = {"account-2": shared_mode.time.time()}
        shared_mode.state_save(state)
        with patch.object(shared_mode, "read_session_usage", side_effect=[(96.0, None, None), (5.0, None, None)]), \
             patch.object(shared_mode, "load_config", return_value=self.config):
            shared_mode.check_usage(self.config_path)
            self.assertEqual(shared_mode.state_read()["last_error"], shared_mode.NO_ACCOUNT_ERROR)
            shared_mode.check_usage(self.config_path)
        updated = shared_mode.state_read()
        self.assertEqual(updated["current"], "account-1")
        self.assertIsNone(updated["last_error"])


    def test_limit_report_follows_the_live_login_instead_of_resting_the_stale_account(self):
        self.enabled_setup()
        self.current_identity = "same@example.com|org-2"  # someone ran /login with account-2
        self.limit()
        state = shared_mode.state_read()
        self.assertEqual(state["current"], "account-2")
        self.assertNotIn("account-1", state.get("limited", {}))
        self.assertEqual(self.logins, [])

    def test_reports_queued_behind_a_failed_attempt_do_not_repeat_it(self):
        self.enabled_setup()
        with patch.object(shared_mode, "claude_login",
                          side_effect=RotatorError("Claude login did not finish within 90 seconds")) as login:
            self.limit()
            self.limit()
        self.assertEqual(login.call_count, 1)  # no rollback login either: the shared login never changed
        self.assertIn("limit_attempt_at", shared_mode.state_read())

    def test_rejected_login_does_not_spend_the_unchanged_shared_login(self):
        state = shared_mode.setup(self.config, self.config_path)
        with patch.object(shared_mode, "claude_login", return_value=False) as login:
            with self.assertRaises(RotatorError):
                shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertEqual(login.call_count, 1)

    def test_login_that_timed_out_but_landed_keeps_the_rotated_token(self):
        state = shared_mode.setup(self.config, self.config_path)
        calls = []
        def login(binary, raw):
            calls.append(raw)
            self.run_claude(["/fake/claude", "auth", "login", "--claudeai"],
                            env={"CLAUDE_CODE_OAUTH_REFRESH_TOKEN": json.loads(raw)["claudeAiOauth"]["refreshToken"]})
            if len(calls) == 1:
                raise RotatorError("Claude login did not finish within 90 seconds")
            return True
        with patch.object(shared_mode, "claude_login", side_effect=login):
            shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertIn("refresh-2-2", self.keychain[shared_mode.vault_service(self.config_path, self.accounts[1])])
        self.assertEqual(shared_mode.state_read()["current"], "account-2")
        self.assertEqual(len(calls), 1)  # one login for the switch, no rollback

    def test_mismatch_does_not_replay_a_spent_token_over_the_previous_login(self):
        # Another process refreshed account-1 and wrote it back over the new login: it is live already, with a
        # newer token, so logging in again with the old one could only fail or revoke it.
        state = shared_mode.setup(self.config, self.config_path)
        def login_then_overwritten(argv, **kwargs):
            result = self.run_claude(argv, **kwargs)
            self.current_identity = "same@example.com|org-1"
            self.keychain[shared_mode.keychain_service(None)] = self.raw(1, 5)
            return result
        with patch.object(shared_mode.subprocess, "Popen", side_effect=login_then_overwritten):
            with self.assertRaisesRegex(RotatorError, "verification failed"):
                shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertEqual(self.logins, ["refresh-2-1"])
        saved = shared_mode.state_read()
        self.assertEqual(saved["current"], "account-1")
        self.assertIn("account-2", saved["login_errors"])

    def test_a_signed_out_shared_login_can_be_switched_back_in(self):
        state = shared_mode.setup(self.config, self.config_path)
        self.current_identity = None
        del self.keychain[shared_mode.keychain_service(None)]
        shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertEqual(shared_mode.state_read()["current"], "account-2")
        self.assertEqual(self.current_identity, "same@example.com|org-2")

    def test_manual_switch_checks_the_live_login_and_clears_a_repaired_error(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["login_errors"] = {"account-2": "Claude rejected the saved login"}
        self.current_identity = "same@example.com|org-2"  # repaired with /login in a terminal
        shared_mode.switch(self.config, self.config_path, state, self.accounts[0], "manual")
        state = shared_mode.state_read()
        self.assertEqual(self.current_identity, "same@example.com|org-1")
        self.assertEqual(state["current"], "account-1")
        self.assertNotIn("account-2", state.get("login_errors", {}))

    def test_prepare_after_upgrade_keeps_rotated_logins_from_older_state(self):
        shared_mode.setup(self.config, self.config_path)
        shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[1], "manual")
        shared_mode.switch(self.config, self.config_path, shared_mode.state_read(), self.accounts[0], "manual")
        vault = shared_mode.vault_service(self.config_path, self.accounts[1])
        state = shared_mode.state_read()
        del state["imported_tokens"]  # written by a release before imported_tokens
        shared_mode.state_save(state)
        shared_mode.setup(self.config, self.config_path)
        self.assertIn("refresh-2-2", self.keychain[vault])
        state = shared_mode.state_read()
        del state["imported_tokens"]
        state["login_errors"] = {"account-2": "Claude rejected the saved login"}  # repaired by signing in again
        shared_mode.state_save(state)
        shared_mode.setup(self.config, self.config_path)
        self.assertIn("refresh-2-1", self.keychain[vault])

    def test_monitor_survives_any_bad_tick_and_clears_its_error_when_healthy(self):
        self.enabled_setup()
        with patch.object(shared_mode, "load_config",
                          side_effect=[subprocess.TimeoutExpired("security", 20), self.config]), \
             patch.object(shared_mode, "read_session_usage", return_value=(10.0, None, None)), \
             patch.object(shared_mode.time, "sleep", side_effect=[None, None, StopIteration]):
            with self.assertRaises(StopIteration):
                shared_mode.watch(None, self.config_path)
        self.assertIsNone(shared_mode.state_read()["last_error"])

    def test_monitor_start_clears_an_earlier_monitor_error(self):
        state = self.enabled_setup()
        state["last_error"] = shared_mode.MONITOR_ERROR + " Could not start the background usage monitor"
        shared_mode.state_save(state)
        with patch.object(shared_mode, "install_hook"), patch.object(shared_mode, "install_agent"), \
             patch("sys.argv", ["shared_mode.py", "--config", str(self.config_path), "ensure-monitor"]):
            shared_mode.main()
        self.assertIsNone(shared_mode.state_read()["last_error"])


    def hold(self, lock, events, label):
        """Hold `lock` on another thread until released; record `label` when done."""
        held, release = threading.Event(), threading.Event()
        def run():
            with platform_runtime.file_lock(lock):
                held.set()
                release.wait(5)
                events.append(label)
        worker = threading.Thread(target=run)
        worker.start()
        held.wait(5)
        threading.Timer(0.5, release.set).start()
        self.addCleanup(worker.join, 5)

    def test_monitor_restart_waits_for_a_running_usage_read(self):
        shared_mode.STATE_DIR.mkdir(parents=True)
        events = []
        self.hold(shared_mode.STATE_DIR / "reader.lock", events, "read done")
        path = self.root / "LaunchAgents" / "switchboard.plist"
        def run(argv, **kwargs):
            if argv[:2] == ["launchctl", "bootout"]:
                events.append("stop")
            return SimpleNamespace(returncode=0)
        with patch.object(shared_mode, "agent_path", return_value=("switchboard.test", path)), \
             patch.object(shared_mode, "sys", self.darwin()), \
             patch.object(shared_mode.os, "getuid", return_value=1000, create=True), \
             patch.object(shared_mode.subprocess, "run", side_effect=run):
            shared_mode.install_agent(self.config_path)
        self.assertEqual(events, ["read done", "stop"])

    def test_retiring_another_macos_install_waits_for_its_switch(self):
        other = self.root / "old" / ".shared-rotator"
        other.mkdir(parents=True)
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        stale = agents / "com.local.switchboard.shared-login.0123456789ab.plist"
        stale.write_bytes(plistlib.dumps({"Label": stale.stem, "StandardOutPath": str(other / "watch.log")}))
        events = []
        self.hold(other / "lock", events, "switch done")
        def run(argv, **kwargs):
            if argv[:2] == ["launchctl", "bootout"] and argv[-1] == str(stale):
                events.append("stopped")
            return SimpleNamespace(returncode=0)
        path = agents / "com.local.switchboard.shared-login.fedcba987654.plist"
        with patch.object(shared_mode, "agent_path", return_value=(path.stem, path)), \
             patch.object(shared_mode, "sys", self.darwin()), \
             patch.object(shared_mode.os, "getuid", return_value=1000, create=True), \
             patch.object(shared_mode.subprocess, "run", side_effect=run):
            shared_mode.install_agent(self.config_path)
        self.assertEqual(events, ["switch done", "stopped"])

    def test_signed_out_login_is_logged_in_even_for_the_account_state_calls_current(self):
        state = shared_mode.setup(self.config, self.config_path)
        self.current_identity = None  # /logout in a terminal
        del self.keychain[shared_mode.keychain_service(None)]
        shared_mode.switch(self.config, self.config_path, state, self.accounts[0], "manual")
        self.assertEqual(self.logins, ["refresh-1-1"])
        self.assertEqual(self.current_identity, "same@example.com|org-1")

    def test_enable_refuses_while_signed_out_and_follows_a_login_to_a_prepared_account(self):
        state = shared_mode.setup(self.config, self.config_path)
        state["current"], state["last_error"] = None, shared_mode.MONITOR_ERROR + " earlier"
        shared_mode.state_save(state)
        argv = ["shared_mode.py", "--config", str(self.config_path), "enable"]
        with patch.object(shared_mode, "load_config", return_value=self.config), \
             patch.object(shared_mode, "install_hook"), patch.object(shared_mode, "install_agent"), \
             patch("sys.argv", argv), patch("sys.stdout", new=io.StringIO()):
            self.current_identity = None
            with self.assertRaisesRegex(RotatorError, "not a prepared account"):
                shared_mode.main()
            self.current_identity = "same@example.com|org-2"  # /login with account-2
            shared_mode.main()
        state = shared_mode.state_read()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["current"], "account-2")
        self.assertIsNone(state["last_error"])

    def test_monitor_follows_a_login_change_even_when_usage_is_unreadable(self):
        self.enabled_setup()
        self.current_identity = "same@example.com|org-2"
        with patch.object(shared_mode, "read_session_usage", return_value=(None, "no screen", None)), \
             patch.object(shared_mode, "load_config", return_value=self.config):
            shared_mode.check_usage(self.config_path)
        self.assertEqual(shared_mode.state_read()["current"], "account-2")

    def test_an_unreadable_saved_login_rests_that_account_so_others_are_tried(self):
        state = self.enabled_setup()
        del self.keychain[shared_mode.vault_service(self.config_path, self.accounts[1])]
        with self.assertRaises(RotatorError):
            shared_mode.switch(self.config, self.config_path, state, self.accounts[1], "manual")
        self.assertTrue(shared_mode.resting(state, "account-2", time.time()))
        self.assertFalse(shared_mode.resting(state, "account-2", time.time() + 16 * 60))

    def test_a_failed_monitor_switch_holds_off_reports_and_its_own_retries(self):
        self.enabled_setup()
        with patch.object(shared_mode, "claude_login",
                          side_effect=RotatorError("Claude login did not finish within 90 seconds")) as login, \
             patch.object(shared_mode, "read_session_usage", return_value=(97.0, None, None)), \
             patch.object(shared_mode, "load_config", return_value=self.config):
            shared_mode.check_usage(self.config_path)
            self.limit()
            shared_mode.check_usage(self.config_path)
        self.assertEqual(login.call_count, 1)

    def test_a_stuck_login_is_ended_once_no_refresh_is_running(self):
        stuck = SimpleNamespace(returncode=None, pid=4242)
        def wait(timeout=None):
            if stuck.returncode is None:
                raise subprocess.TimeoutExpired("claude", timeout)
        stuck.wait = wait
        signals = []
        def interrupt(process, force=False):
            signals.append(force)
            if force:
                process.returncode = -9
        clock = iter(range(0, 100000, 30))
        with patch.object(shared_mode, "popen_detached", return_value=stuck), \
             patch.object(shared_mode, "interrupt_process", side_effect=interrupt), \
             patch.object(shared_mode.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(RotatorError, "did not finish"):
                shared_mode.claude_login("/fake/claude", self.raw(2, 1))
        self.assertEqual(signals, [False, True])


if __name__ == "__main__":
    unittest.main()
