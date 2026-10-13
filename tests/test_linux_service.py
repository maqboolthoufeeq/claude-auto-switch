"""Opt-in integration test using only disposable services and no Claude accounts."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import background_monitor


@unittest.skipUnless(sys.platform.startswith("linux") and os.environ.get("SWITCHBOARD_TEST_SYSTEMD") == "1",
                     "Requires an explicitly enabled systemd user integration environment")
class LinuxServiceTests(unittest.TestCase):
    def test_start_restart_and_stop_entire_service_group(self):
        with tempfile.TemporaryDirectory(prefix="switchboard-service-") as root:
            directory = Path(root)
            config = directory / "project with spaces" / "config.json"
            config.parent.mkdir()
            marker = config.with_suffix(".pid")
            script = directory / "harmless monitor.py"
            script.write_text(
                "import os, subprocess, sys, time\nfrom pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(180)'])\n"
                "Path(sys.argv[2]).with_suffix('.pid').write_text(str(os.getpid()) + ',' + str(child.pid))\n"
                "time.sleep(180)\n", encoding="utf-8")
            def wait_start(previous=None):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if marker.exists():
                        value = marker.read_text()
                        if value and value != previous:
                            return value
                    time.sleep(0.1)
                self.fail("The service did not execute the disposable monitor")
            try:
                background_monitor.install(config, directory, script)
                first = wait_start()
                background_monitor.install(config, directory, script)
                second = wait_start(first)
                self.assertNotEqual(first, second)
                background_monitor.remove(config, directory)
                for pid in map(int, (first + ',' + second).split(',')):
                    deadline = time.monotonic() + 5
                    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                        time.sleep(0.1)
                    self.assertFalse(Path(f"/proc/{pid}").exists(), "The service left a child running")
                self.assertFalse(background_monitor.linux_unit_path(config).exists())
            finally:
                if background_monitor.linux_unit_path(config).exists():
                    background_monitor.remove(config, directory)
