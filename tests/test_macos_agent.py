"""Opt-in LaunchAgent probe; never opens Claude or reads an account."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import shared_mode


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("SWITCHBOARD_TEST_LAUNCHD") == "1",
                     "Requires an explicitly enabled macOS LaunchAgent integration environment")
class MacAgentTests(unittest.TestCase):
    def test_native_launchagent_starts_replaces_and_stops(self):
        with tempfile.TemporaryDirectory(prefix="switchboard-launchd-") as root:
            directory = Path(root)
            config = directory / "project with spaces" / "config.json"
            config.parent.mkdir()
            marker = config.with_suffix(".pid")
            script = directory / "harmless monitor.py"
            script.write_text(
                "import os,sys,time\nfrom pathlib import Path\n"
                "Path(sys.argv[2]).with_suffix('.pid').write_text(str(os.getpid()))\n"
                "time.sleep(180)\n", encoding="utf-8")
            label, _ = shared_mode.agent_path(config)
            plist = directory / (label + ".plist")
            def wait_start(previous=None):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if marker.exists():
                        value = marker.read_text()
                        if value and value != previous:
                            return int(value)
                    time.sleep(0.1)
                self.fail("LaunchAgent did not execute the harmless monitor")
            def stopped(pid):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return
                    time.sleep(0.1)
                self.fail("LaunchAgent did not stop its disposable monitor")
            with patch.object(shared_mode, "__file__", str(script)), \
                 patch.object(shared_mode, "STATE_DIR", directory), \
                 patch.object(shared_mode, "agent_path", return_value=(label, plist)):
                try:
                    shared_mode.install_agent(config)
                    first = wait_start()
                    shared_mode.install_agent(config)
                    second = wait_start(str(first))
                    self.assertNotEqual(first, second)
                    stopped(first)
                    shared_mode.remove_agent(config)
                    stopped(second)
                    self.assertFalse(plist.exists())
                finally:
                    shared_mode.remove_agent(config)
