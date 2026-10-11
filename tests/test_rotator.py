import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

account = Path(os.environ["CLAUDE_CONFIG_DIR"]).name
trace = Path(os.environ["FAKE_TRACE_DIR"])
trace.mkdir(exist_ok=True)
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    sys.exit(0)
if sys.argv[1:3] == ["auth", "login"]:
    (trace / "login.json").write_text(json.dumps({"account": account, "argv": sys.argv[1:]}))
    sys.exit(0)
if "-p" not in sys.argv:
    sys.exit(4)
with (trace / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps({"account": account, "argv": sys.argv[1:]}) + "\n")
mode = os.environ.get("FAKE_MODE", "limit")
if mode == "limit" and account == "account-1":
    print(json.dumps({"is_error": True, "result": "You've hit your session limit · resets at 3pm"}))
    sys.exit(1)
if mode == "error":
    print(json.dumps({"is_error": True, "result": "Network unavailable"}))
    sys.exit(1)
if mode == "continue":
    count_path = trace / "count"
    count = int(count_path.read_text()) if count_path.exists() else 0
    count_path.write_text(str(count + 1))
    if count == 0:
        print(json.dumps({"is_error": False, "session_id": "session-1", "structured_output": {
            "status": "continue", "summary": "First step done", "next_steps": "Finish"}}))
        sys.exit(0)
    if "--resume" not in sys.argv or "session-1" not in sys.argv:
        sys.exit(5)
print(json.dumps({"is_error": False, "session_id": "session-2", "structured_output": {
    "status": "complete", "summary": "Task done", "next_steps": ""}}))
'''
FAKE_USAGE = r'''#!/usr/bin/env python3
import os
from pathlib import Path
print("96" if Path(os.environ["CLAUDE_CONFIG_DIR"]).name == "account-1" else "10")
'''


class RotatorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.trace = self.root / "trace"
        self.fake = self.root / "claude"
        self.fake.write_text(FAKE_CLAUDE)
        self.fake.chmod(0o755)
        self.usage = self.root / "usage"
        self.usage.write_text(FAKE_USAGE)
        self.usage.chmod(0o755)
        self.config = self.root / "rotator.json"
        self.accounts = [
            {"name": f"account-{n}", "config_dir": str(self.root / f"account-{n}"),
             "threshold_percent": 95, "usage_command": None}
            for n in (1, 2)
        ]
        self.write_config()

    def write_config(self):
        self.config.write_text(json.dumps({"claude_command": str(self.fake),
                                           "permission_mode": "auto", "max_runs": 5,
                                           "accounts": self.accounts}))

    def call(self, *args, mode="limit"):
        env = os.environ.copy()
        env["FAKE_TRACE_DIR"] = str(self.trace)
        env["FAKE_MODE"] = mode
        return subprocess.run([sys.executable, "-B", str(ROOT / "rotator.py"),
                               "--config", str(self.config), *args],
                              env=env, capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in (self.trace / "calls.jsonl").read_text().splitlines()]

    def state(self):
        return json.loads((self.project / ".claude-rotator" / "state.json").read_text())

    def test_limit_rotates_to_second_account(self):
        result = self.call("run", "--project", str(self.project), "Build a feature")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call["account"] for call in self.calls()], ["account-1", "account-2"])
        self.assertEqual(self.state()["status"], "complete")
        self.assertEqual(self.state()["runs"][0]["outcome"], "usage_limit")
        self.assertIn("Build a feature", (self.project / ".claude-rotator" / "HANDOFF.md").read_text())

    def test_threshold_skips_account_before_run(self):
        for account in self.accounts:
            account["usage_command"] = [str(self.usage)]
        self.write_config()
        result = self.call("run", "--project", str(self.project), "Build a feature")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call["account"] for call in self.calls()], ["account-2"])
        self.assertIn("96% used", result.stdout)

    def test_continue_resumes_same_session(self):
        result = self.call("run", "--project", str(self.project), "Build a feature", mode="continue")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call["account"] for call in self.calls()], ["account-1", "account-1"])
        self.assertIn("--resume", self.calls()[1]["argv"])

    def test_other_error_pauses_without_rotating(self):
        result = self.call("run", "--project", str(self.project), "Build a feature", mode="error")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([call["account"] for call in self.calls()], ["account-1"])
        self.assertEqual(self.state()["status"], "paused")

    def test_profiles_inside_project_resolve_from_config_and_are_ignored(self):
        self.config = self.project / "rotator.json"
        for account in self.accounts:
            account["config_dir"] = f"profile/{account['name']}"
        self.accounts[0]["email"] = "one@example.com"
        self.write_config()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)

        login = self.call("login", "account-1")
        self.assertEqual(login.returncode, 0, login.stdout + login.stderr)
        self.assertTrue((self.project / "profile" / "account-1").is_dir())
        self.assertEqual((self.project / "profile" / "account-1").stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((self.trace / "login.json").read_text())["argv"],
                         ["auth", "login", "--email", "one@example.com"])

        result = self.call("run", "--project", str(self.project), "Build a feature")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call["account"] for call in self.calls()], ["account-1", "account-2"])
        ignore = (self.project / ".gitignore").read_text()
        self.assertIn("/profile/account-1/", ignore)
        self.assertIn("/profile/account-2/", ignore)


if __name__ == "__main__":
    unittest.main()
