"""Exercise real terminal I/O with a harmless fake Claude screen."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from session_usage import read_session_usage
from test_helpers import write_executable


PROGRAM = r'''#!/usr/bin/env python3
import sys,time
sys.stdout.write("\x1b[2J\x1b[HClaude Code vtest\r\n$ ")
sys.stdout.flush()
if sys.stdin.readline().strip() != "/usage":
    raise SystemExit(1)
sys.stdout.write("\x1b[2J\x1b[HCurrent session\r\n0% used\r\nRefreshing...")
sys.stdout.flush()
time.sleep(.1)
sys.stdout.write("\x1b[2J\x1b[HCurrent session\r\n6% used\r\nCurrent week\r\n90% used")
sys.stdout.flush()
time.sleep(60)
'''


class NativeTerminalTests(unittest.TestCase):
    def test_real_terminal_refresh_replaces_old_percentage(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "project with spaces"
            directory.mkdir()
            path = directory / "fake-usage"
            if sys.platform == "win32":
                # Run the real terminal adapter inside the same job used by the background monitor.
                from platform_runtime import windows_monitor_job
                windows_monitor_job()
                from winpty import PTY as RealPTY
                script = path.with_suffix(".py")
                script.write_text(PROGRAM)
                class TestPTY:
                    def __init__(self, cols, rows):
                        self.process = RealPTY(cols, rows)
                    def spawn(self, binary, **kwargs):
                        kwargs["cmdline"] = " " + subprocess.list2cmdline(["-u", str(script)])
                        return self.process.spawn(sys.executable, **kwargs)
                    def __getattr__(self, name):
                        return getattr(self.process, name)
                with patch.dict(sys.modules, {"winpty": SimpleNamespace(PTY=TestPTY)}):
                    result = read_session_usage("fake-claude.exe", dict(os.environ, CLAUDE_CONFIG_DIR=root), timeout=15)
            else:
                binary = write_executable(path, PROGRAM)
                # Its own config folder: the reader must not wait on the developer's real ~/.claude refresh lock.
                result = read_session_usage(str(binary), dict(os.environ, CLAUDE_CONFIG_DIR=root), timeout=15)
            self.assertEqual(result, (6.0, None, None))


if __name__ == "__main__":
    unittest.main()
