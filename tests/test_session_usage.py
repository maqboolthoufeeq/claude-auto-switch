import calendar
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import unittest.mock
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

    @unittest.skipIf(sys.platform == "win32", "time.tzset")
    def test_reset_after_a_daylight_saving_change_keeps_the_wall_clock_time(self):
        previous = os.environ.get("TZ")
        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        now = calendar.timegm((2026, 11, 1, 3, 30, 0))  # 23:30 EDT, the night clocks go back
        self.assertEqual(parse_session_reset("Current session\n96% used\nResets 3am\nCurrent week", now),
                         calendar.timegm((2026, 11, 1, 8, 0, 0)))  # 03:00 EST

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


@unittest.skipIf(sys.platform == "win32", "pty-based check")
class RefreshWaitTests(unittest.TestCase):
    def test_reader_keeps_draining_while_a_login_refresh_finishes(self):
        # A Claude that keeps drawing while it refreshes must not block on a full terminal and be killed mid-refresh.
        with tempfile.TemporaryDirectory() as folder:
            lock, done = Path(folder) / ".oauth_refresh.lock", Path(folder) / "refresh-saved"
            fake = Path(folder) / "claude"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import os, sys, time\n"
                "from pathlib import Path\n"
                "sys.stdout.write('Claude Code v9.9 test build\\n$ '); sys.stdout.flush()\n"
                "for line in sys.stdin:\n"
                "    if line.replace('\\x1b', '').strip() == '/usage':\n"
                "        sys.stdout.write('Current session\\n42% 42% used\\nCurrent week\\n'); sys.stdout.flush()\n"
                "    elif '/exit' in line:\n"
                "        break\n"
                "lock = Path(os.environ['CLAUDE_CONFIG_DIR']) / '.oauth_refresh.lock'\n"
                "lock.write_text('refreshing')\n"
                "end = time.monotonic() + 1.5\n"
                "while time.monotonic() < end:\n"
                "    sys.stdout.write('x' * 2048); sys.stdout.flush()  # blocks unless the reader drains the pty\n"
                "(Path(os.environ['CLAUDE_CONFIG_DIR']) / 'refresh-saved').write_text('ok')\n"
                "lock.unlink()\n")
            fake.chmod(0o700)
            env = {"PATH": os.environ.get("PATH", ""), "CLAUDE_CONFIG_DIR": folder}
            with unittest.mock.patch("session_usage.stop_claude.__defaults__", (0.5,)):
                self.assertEqual(read_session_usage(str(fake), env, Path(folder)), (42.0, None, None))
            self.assertTrue(done.exists())
            self.assertFalse(lock.exists())
