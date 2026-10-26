from datetime import datetime
import unittest
from mailbot.task_export import task_payload


class TaskExportTests(unittest.TestCase):
    def test_preserves_japan_calendar_date_across_utc_boundary(self):
        ts = int(datetime.fromisoformat('2026-10-25T00:30:00+09:00').timestamp())
        result = task_payload({'id': 'demo', 'title': 'test', 'due_at': ts, 'completed': False})
        self.assertEqual(result['payload']['due'], '2026-10-25T00:00:00Z')
        self.assertEqual(result['local_reminder_at'], '2026-10-24T15:30:00+00:00')
        self.assertFalse(result['sent'])

    def test_no_due_and_completed(self):
        result = task_payload({'title': 'done', 'completed': True})
        self.assertNotIn('due', result['payload'])
        self.assertEqual(result['payload']['status'], 'completed')

    def test_rejects_invalid_inputs(self):
        for task in ({'title': ''}, {'title': 'x', 'due_at': True}):
            with self.assertRaises(ValueError):
                task_payload(task)
