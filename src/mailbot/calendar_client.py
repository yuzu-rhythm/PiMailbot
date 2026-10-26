"""Read-only Calendar REST access using the existing private Google OAuth transport."""
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request

from .gmail_client import GmailClient, GmailAuthError, GmailProtocolError

CALENDAR_SCOPES = (
    'https://www.googleapis.com/auth/calendar.events.readonly',
    'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
)


class CalendarClient(GmailClient):
    """Only GET requests to Calendar; no event creation/update/delete interface."""
    def __init__(self, token_source, **kwargs):
        super().__init__(token_source, **kwargs)
        scopes = self._credentials.get('scopes', [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not set(CALENDAR_SCOPES).issubset(scopes):
            raise GmailAuthError('Calendar read authorization required')

    @staticmethod
    def _request(path, params, token, body=None):
        if body is not None or not path.startswith(('/calendars/', '/users/me/calendarList')):
            raise ValueError('Calendar client only permits read requests')
        query = urlencode(params or {})
        return Request('https://www.googleapis.com/calendar/v3' + path + ('?' + query if query else ''),
                       headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, method='GET')

    def calendars(self):
        rows, page = [], None
        for _ in range(10):
            params = {'maxResults': 250, 'showHidden': 'true'}
            if page:
                params['pageToken'] = page
            result = self._get_json('/users/me/calendarList', params)
            if not isinstance(result.get('items'), list):
                raise GmailProtocolError('Invalid calendar list')
            rows.extend(result['items'])
            page = result.get('nextPageToken')
            if not page:
                return rows
        raise GmailProtocolError('Calendar list exceeds collection limit')

    def check_selection(self, expected_email, selected):
        rows = self.calendars()
        primary = [row.get('id', '').casefold() for row in rows if row.get('primary')]
        if primary != [expected_email.casefold()]:
            raise GmailAuthError('Calendar account identity mismatch')
        visible = {row['id'] for row in rows if row.get('accessRole') in {'reader', 'writer', 'owner'}}
        if any(row['id'] not in visible for row in selected):
            raise GmailAuthError('Selected calendar is unavailable')

    def events(self, calendar_id, start, end):
        rows, page = [], None
        for _ in range(5):
            params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'timeZone': 'Asia/Tokyo',
                      'singleEvents': 'true', 'orderBy': 'startTime', 'showDeleted': 'false', 'maxResults': 100,
                      'fields': 'items(id,status,summary,location,start,end,attendees(self,responseStatus)),nextPageToken'}
            if page:
                params['pageToken'] = page
            result = self._get_json('/calendars/' + quote(calendar_id, safe='') + '/events', params)
            if not isinstance(result.get('items', []), list):
                raise GmailProtocolError('Invalid calendar events')
            rows.extend(result.get('items', []))
            page = result.get('nextPageToken')
            if not page:
                return rows, False
        return rows, True


def normalized_event(row, selection, timezone):
    """Keep only display data; all-day end dates are exclusive, per Google."""
    if row.get('status') == 'cancelled':
        return None
    if any(a.get('self') and a.get('responseStatus') == 'declined' for a in row.get('attendees', [])):
        return None
    start, end = row.get('start', {}), row.get('end', {})
    all_day = 'date' in start
    if all_day:
        start_at, end_at = start['date'], end['date']
        datetime.strptime(start_at, '%Y-%m-%d')
        datetime.strptime(end_at, '%Y-%m-%d')
    else:
        start_dt = datetime.fromisoformat(start['dateTime'].replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end['dateTime'].replace('Z', '+00:00'))
        if start_dt.tzinfo is None or end_dt.tzinfo is None:
            raise ValueError('Event timezone missing')
        start_at, end_at = start_dt.astimezone(timezone).isoformat(), end_dt.astimezone(timezone).isoformat()
    if end_at <= start_at:
        raise ValueError('Invalid event interval')
    return {'title': str(row.get('summary') or '予定あり')[:300], 'start': start_at, 'end': end_at,
            'all_day': all_day, 'calendar': selection['key'], 'label': selection['label'],
            'location': str(row.get('location') or '')[:200]}
