"""Build Google Tasks compatible payloads without sending any data."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def task_payload(task: dict, *, local_zone: str = 'Asia/Tokyo') -> dict:
    """Keep reminder time separate from the API's date-only due field."""
    title = task.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError('task title must be 1 to 500 characters')
    zone = ZoneInfo(local_zone)
    payload = {'title': title, 'status': 'completed' if task.get('completed') else 'needsAction'}
    reminder_at = None
    due_at = task.get('due_at')
    if due_at is not None:
        if isinstance(due_at, bool) or not isinstance(due_at, (int, float)):
            raise ValueError('due_at must be a Unix timestamp')
        due = datetime.fromtimestamp(due_at, timezone.utc)
        # Google Tasks stores a calendar date. Preserve the user's Japan date,
        # even when UTC is on the previous day.
        payload['due'] = due.astimezone(zone).date().isoformat() + 'T00:00:00Z'
        reminder_at = due.isoformat()
    return {
        'mode': 'OFFLINE_EXPORT_ONLY',
        'google_tasks': 'NOT_CONFIGURED',
        'local_task_id': task.get('id'),
        'payload': payload,
        'local_reminder_at': reminder_at,
        'calendar_zone': local_zone,
        'sent': False,
    }
