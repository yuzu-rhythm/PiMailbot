import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MailCliTests(unittest.TestCase):
    def call(self, database, *args):
        return subprocess.run(
            [sys.executable, '-m', 'mailbot', 'mail', '--mail-db', str(database), *args],
            cwd=ROOT, env={'PYTHONPATH': str(ROOT / 'src')},
            capture_output=True, text=True, timeout=10,
        )

    def test_demo_survives_subprocess_restart_without_duplicate_actions(self):
        with tempfile.TemporaryDirectory() as d:
            database = Path(d) / 'mail.sqlite3'
            for _ in range(2):
                result = self.call(database, 'demo')
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['mode'], 'SIMULATED')
                self.assertEqual(output['status']['message_count'], 1)
                self.assertEqual(output['status']['simulated_delivery_count'], 1)
                self.assertEqual(output['status']['pending_action_count'], 1)
            status = json.loads(self.call(database, 'status').stdout)
            self.assertEqual(status['integrations']['gmail'], 'NOT_CONFIGURED')

    def test_manual_delivery_requires_policy_even_after_demo(self):
        with tempfile.TemporaryDirectory() as d:
            database = Path(d) / 'mail.sqlite3'
            self.assertEqual(self.call(database, 'demo').returncode, 0)
            result = self.call(database, 'simulate-delivery', 'offline-demo-mail-001',
                               '--guild', '101', '--channel', '202', '--user', '303')
            self.assertEqual(result.returncode, 2)
            self.assertIn('not allowlisted', result.stderr)
