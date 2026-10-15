import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import pyte

from session_usage import parse_session_reset, parse_session_usage, read_session_usage


class SessionUsageTests(unittest.TestCase):
    def test_reads_current_session_not_week_or_context(self):
        screen = (
            "Context window 71% used\r\n"
            "Settings  Status  Usage\r\n"
            "Current session\r\n"
            "\x1b[2K95% 95% used\r\n"
            "Resets 7:10am\r\n"
            "Current week\r\n10% 10% used\r\n"
        )
        self.assertEqual(parse_session_usage(screen), 95)

    def test_rejects_missing_or_invalid_session_number(self):
        self.assertIsNone(parse_session_usage("Current week\n95% used"))
        self.assertIsNone(parse_session_usage("Current session\n101% used"))
        self.assertIsNone(parse_session_usage("Current session\nUnavailable\nCurrent week\n95% used"))

    def test_terminal_redraw_uses_new_session_percentage(self):
        screen = pyte.Screen(120,120)
        stream = pyte.Stream(screen)
        stream.feed("Current session\r\n0% 0% used\r\nCurrent week\r\n90% used")
        stream.feed("\x1b[2;1H\x1b[2K6% 6% used")
        self.assertEqual(parse_session_usage("\n".join(screen.display)), 6)

    def test_reads_session_reset_time_but_not_the_weekly_one(self):
        now = time.mktime((2026, 10, 15, 13, 0, 0, 0, 0, -1))
        def clock(text):
            reset = parse_session_reset(text, now)
            return reset and time.strftime("%H:%M", time.localtime(reset))
        self.assertEqual(clock("Current session\n95% used\nResets 3pm (Europe/Berlin)\nCurrent week\nResets Oct 18, 9am"), "15:00")
        self.assertEqual(clock("Current session\r\n95% 95% used\r\nResets 2:30pm\r\nCurrent week"), "14:30")
        self.assertEqual(clock("Current session\n95% used\nResets 15:00"), "15:00")
        self.assertEqual(clock("Current session\n95% used\nResets Oct 15, 5pm"), "17:00")
        for text in ("Current session\n95% used\nResets 12pm",  # a day away: not a session window
                     "Current session\n95% used\nResets in 2h 30m",
                     "Current session\n95% used\nCurrent week\nResets 3pm"):
            self.assertIsNone(parse_session_reset(text, now))


@unittest.skipIf(sys.platform == "win32", "pty-based check")
class GracefulExitTests(unittest.TestCase):
    def test_reader_lets_claude_exit_on_its_own(self):
        # A killed Claude can die mid token-refresh and break every other session's login.
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "clean-exit"
            fake = Path(folder) / "claude"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "sys.stdout.write('Claude Code v9.9 test build\\n$ '); sys.stdout.flush()\n"
                "for line in sys.stdin:\n"
                "    text = line.replace('\\x1b', '').strip()\n"
                "    if text == '/usage':\n"
                "        sys.stdout.write('Settings  Status  Usage\\nCurrent session\\n42% 42% used\\nCurrent week\\n10% used\\n')\n"
                "        sys.stdout.flush()\n"
                "    elif text == '/exit':\n"
                "        open(os.environ['FAKE_MARKER'], 'w').write('clean'); sys.exit(0)\n")
            fake.chmod(0o700)
            env = {"PATH": os.environ.get("PATH", ""), "FAKE_MARKER": str(marker), "CLAUDE_CONFIG_DIR": folder}
            self.assertEqual(read_session_usage(str(fake), env, Path(folder)), (42.0, None, None))
            self.assertEqual(marker.read_text(), "clean")
