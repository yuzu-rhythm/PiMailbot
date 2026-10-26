from pathlib import Path
import unittest
from mailbot.config import Settings, ConfigurationError


class GmailConfigTests(unittest.TestCase):
    def test_default_disabled_and_credentials_not_in_status(self):
        settings = Settings.from_env({})
        self.assertFalse(settings.gmail_enabled)
        self.assertIsNone(settings.gmail_token_path)
        self.assertEqual(settings.gmail_poll_seconds, 90)

    def test_enabled_requires_explicit_setting(self):
        settings = Settings.from_env({'MAILBOT_GMAIL_ENABLED': 'true', 'MAILBOT_GMAIL_TOKEN_PATH': '/private/google.json'})
        self.assertTrue(settings.gmail_enabled)
        self.assertEqual(settings.gmail_token_path, Path('/private/google.json'))
        self.assertNotIn('/private/google.json', str(settings.public_dict()))

    def test_poll_interval_and_flags_fail_closed(self):
        for config in ({'MAILBOT_GMAIL_ENABLED':'maybe'}, {'MAILBOT_GMAIL_POLL_SECONDS':'1'}, {'MAILBOT_GMAIL_POLL_SECONDS':'99999'}):
            with self.assertRaises(ConfigurationError): Settings.from_env(config)
