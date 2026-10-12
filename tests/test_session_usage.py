import json
from pathlib import Path
import tempfile
import unittest

from session_usage import cached_session_usage, parse_session_usage


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

    def test_cache_requires_current_identity_recent_timestamp_and_session_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            value = {"oauthAccount": {"accountUuid": "test-account"}, "cachedUsageUtilization": {
                "accountUuid": "test-account", "fetchedAtMs": 100000,
                "utilization": {"five_hour": {"utilization": 94.5}, "seven_day": {"utilization": 99}}}}
            path.write_text(json.dumps(value))
            self.assertEqual(cached_session_usage(path, 110), 94.5)
            self.assertIsNone(cached_session_usage(path, 161))
            value["cachedUsageUtilization"]["accountUuid"] = "different-account"
            path.write_text(json.dumps(value))
            self.assertIsNone(cached_session_usage(path, 110))
