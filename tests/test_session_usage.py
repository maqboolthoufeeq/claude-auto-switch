import unittest
import pyte

from session_usage import parse_session_usage


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
