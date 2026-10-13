import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import xml.etree.ElementTree as ET

import background_monitor
import launch
import platform_runtime
import secure_store
import session_usage
import shared_mode
from rotator import RotatorError


class PlatformTests(unittest.TestCase):
    def test_launcher_reopens_existing_private_dashboard(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / ".shared-rotator").mkdir()
            link = "http://127.0.0.1:56729/#token=test-only"
            (directory / ".shared-rotator" / "dashboard-url").write_text(link)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b"<title>Switchboard</title>"
            with patch.object(launch, "BASE_DIR", directory), \
                 patch.object(launch.urllib.request, "urlopen", return_value=response), \
                 patch.object(launch.webbrowser, "open") as browser, \
                 patch.object(launch, "dashboard_main") as start:
                self.assertEqual(launch.main(), 0)
                browser.assert_called_once_with(link)
                start.assert_not_called()

    def test_launcher_creates_first_config_in_its_project_not_working_directory(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "app"
            directory.mkdir()
            elsewhere = Path(root) / "other project"
            elsewhere.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(elsewhere)
                with patch.object(launch, "BASE_DIR", directory), \
                     patch.object(launch, "dashboard_main", return_value=0), \
                     patch("sys.stdout", new=io.StringIO()):
                    self.assertEqual(launch.main(), 0)
                self.assertTrue((directory / "rotator.json").is_file())
                self.assertFalse((elsewhere / "rotator.json").exists())
            finally:
                os.chdir(previous)

    def test_lock_rejects_a_concurrent_run_and_releases(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "lock"
            with platform_runtime.file_lock(path):
                code = ("from pathlib import Path; from platform_runtime import file_lock; "
                        f"\nwith file_lock(Path({str(path)!r}), blocking=False): pass")
                result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"BlockingIOError", result.stderr)
            with platform_runtime.file_lock(path, blocking=False):
                pass

    def test_linux_service_quotes_paths_and_excludes_auth_environment(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "project with $name and % /config.json"
            unit = Path(root) / "switchboard-test.service"
            env = {"PATH": "/usr/bin:/home/test/.local/bin", "ANTHROPIC_API_KEY": "do-not-save"}
            with patch.object(background_monitor.sys, "platform", "linux"), \
                 patch.object(background_monitor, "linux_unit_path", return_value=unit), \
                 patch.dict(os.environ, env), \
                 patch.object(background_monitor, "run_checked") as run:
                background_monitor.install(config, Path(root), Path(root) / "shared_mode.py")
                content = unit.read_text()
                self.assertIn("$$name", content)
                self.assertIn("%%", content)
                self.assertNotIn("do-not-save", content)
                self.assertIn("Restart=on-failure", content)
                self.assertEqual(run.call_args.args[0][-2:], ["restart", unit.name])
                background_monitor.remove(config, Path(root))
                self.assertFalse(unit.exists())

    def test_windows_task_uses_interactive_user_without_password_or_admin(self):
        argv = ["C:\\My App\\python.exe", "C:\\My App\\shared_mode.py", "--config", "C:\\Project & Work\\rotator.json", "watch"]
        with patch.object(background_monitor.subprocess, "check_output", return_value='"TEST\\user","S-1-5-21-123"\n'):
            data = background_monitor.windows_task(argv)
        root = ET.fromstring(data)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns), "InteractiveToken")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns), subprocess.list2cmdline(argv[1:]))
        self.assertNotIn("Password", data.decode("utf-16"))

    def test_windows_hook_quotes_special_paths_for_powershell(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Path(root) / "settings.json"
            config = Path(root) / "Project $name's files" / "rotator.json"
            with patch.object(shared_mode.sys, "platform", "win32"):
                shared_mode.install_hook(config, settings)
            item = json.loads(settings.read_text())["hooks"]["StopFailure"][0]["hooks"][0]
            self.assertEqual(item["shell"], "powershell")
            self.assertIn("$name''s files", item["command"])
            self.assertTrue(item["command"].startswith("& '"))

    def test_non_mac_vault_roundtrips_long_credentials_using_protected_backend(self):
        items = {}
        store = SimpleNamespace(get_password=lambda name,user: items.get(name),
                                set_password=lambda name,user,value: items.__setitem__(name,value),
                                delete_password=lambda name,user: items.pop(name))
        raw = json.dumps({"claudeAiOauth": {"refreshToken": "test-" * 3000, "scopes": ["user:inference"]}})
        with patch.object(shared_mode.sys, "platform", "linux"), \
             patch.object(secure_store, "backend", return_value=store):
            shared_mode.keychain_write("Switchboard-v4-test-protected", raw)
            self.assertEqual(shared_mode.vault_read("Switchboard-v4-test-protected"), raw)
            shared_mode.vault_delete("Switchboard-v4-test-protected")
        self.assertEqual(items, {})

    def test_locked_secure_backend_has_no_plaintext_fallback(self):
        store = Mock()
        store.set_password.side_effect = RuntimeError("locked, private detail")
        with patch.object(secure_store, "backend", return_value=store):
            with self.assertRaises(RotatorError) as error:
                secure_store.write("test", b"private-value")
        self.assertNotIn("private detail", str(error.exception))

    def test_non_mac_reads_claudes_own_profile_file(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            raw = '{"claudeAiOauth": {"refreshToken": "test", "scopes": ["user:inference"]}}'
            (directory / ".credentials.json").write_text(raw)
            with patch.object(shared_mode.sys, "platform", "linux"), \
                 patch.object(shared_mode, "keychain_read") as mac_read:
                self.assertEqual(shared_mode.claude_credential(directory), raw)
                mac_read.assert_not_called()

    def test_windows_usage_adapter_reads_refreshed_screen_and_cleans_up(self):
        process = Mock()
        process.spawn.return_value = True
        process.read.side_effect = ["Claude Code vtest\r\n$ ",
                                    "\x1b[2J\x1b[HCurrent session\r\n0% used\r\nRefreshing…",
                                    "\x1b[2J\x1b[HCurrent session\r\n6% used\r\nCurrent week\r\n90% used"] + [""] * 100
        process.isalive.return_value = True
        process.pid = 98765
        counter = iter(n / 4 for n in range(200))
        with patch.object(session_usage.sys, "platform", "win32"), \
             patch.dict(sys.modules, {"winpty": SimpleNamespace(PTY=Mock(return_value=process))}), \
             patch.object(session_usage.time, "monotonic", side_effect=lambda: next(counter)), \
             patch.object(session_usage.time, "sleep"), \
             patch.object(session_usage, "kill_windows_tree") as cleanup:
            result = session_usage.read_session_usage("claude.exe", {})
        self.assertEqual(result, (6.0, None))
        process.write.assert_called_once_with("/usage\r")
        cleanup.assert_called_once_with(98765)

    @unittest.skipUnless(sys.platform == "win32", "Requires the native Windows credential store")
    def test_native_windows_credential_store_roundtrip(self):
        import uuid
        name = "Switchboard-v4-test-" + uuid.uuid4().hex
        try:
            raw = "test-only-value-" * 1000
            shared_mode.keychain_write(name, raw)
            self.assertEqual(shared_mode.vault_read(name), raw)
            from win32ctypes.pywin32 import win32cred
            item = win32cred.CredRead(Type=win32cred.CRED_TYPE_GENERIC, TargetName=name)
            self.assertEqual(item["Persist"], win32cred.CRED_PERSIST_LOCAL_MACHINE)
        finally:
            shared_mode.vault_delete(name)

    @unittest.skipUnless(sys.platform == "win32", "Requires native Windows folder permissions")
    def test_native_windows_private_folder_allows_only_user_and_system(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "private folder with spaces"
            real_run = subprocess.run
            def diagnostic_run(*args, **kwargs):
                result = real_run(*args, **kwargs)
                if result.returncode:
                    # This test uses only disposable paths and a static ACL script, never account data.
                    detail = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
                    print("Disposable ACL test diagnostic:", detail[-2500:])
                return result
            with patch.object(platform_runtime.subprocess, "run", side_effect=diagnostic_run):
                platform_runtime.private_directory(path)
            script = (
                "$acl = Get-Acl -LiteralPath $env:SWITCHBOARD_TEST_PATH; "
                "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                "$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])); "
                "$bad = @($rules | Where-Object { $_.IdentityReference.Value -notin @($user, 'S-1-5-18') }); "
                "if ($acl.AreAccessRulesProtected -and $rules.Count -eq 2 -and $bad.Count -eq 0) { exit 0 } else { exit 1 }"
            )
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                    env=dict(platform_runtime.powershell_env(), SWITCHBOARD_TEST_PATH=str(path)), capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, "Private-folder ACL did not restrict access to the expected principals")

    @unittest.skipUnless(sys.platform == "win32", "Requires native Windows Task Scheduler")
    def test_native_windows_task_xml_can_register_without_a_password(self):
        import uuid
        name = "switchboard-test-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "task.xml"
            path.write_bytes(background_monitor.windows_task([sys.executable, "-c", "pass"]))
            try:
                result = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"],
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, "The current-user task XML could not be registered")
            finally:
                subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
