"""Bounded durable Todoist synchronization, disabled unless explicitly enabled."""
import asyncio
import uuid

from .todoist_client import TodoistClient, TodoistError


class TodoistSync:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.client = None
        self.state = 'DISABLED'
        self.last_error = None
        self.retry_after = 0
        store.configure_todoist(settings.todoist_enabled)
        if settings.todoist_enabled:
            self.state = 'NOT_CONFIGURED'
            if settings.todoist_token_path is not None:
                try:
                    self.client = TodoistClient(settings.todoist_token_path)
                    self.state = 'CONFIGURED_NOT_CONNECTED'
                except TodoistError as error:
                    self.last_error = error.code
                    self.retry_after = error.retry_after

    def status(self):
        return {'state': self.state, 'last_error': self.last_error,
                'queue': self.store.todoist_counts()}

    def cycle(self):
        if self.client is None or self.state == 'AUTH_REQUIRED':
            return
        self.retry_after = 0
        try:
            self.client.check_connection()
        except TodoistError as error:
            self.state = 'AUTH_REQUIRED' if error.code == 'AUTH_REQUIRED' else 'ERROR'
            self.last_error = error.code
            self.retry_after = error.retry_after
            return
        self.state, self.last_error = 'READY', None
        for task in self.store.todoist_pending():
            creating = task['remote_id'] is None
            try:
                if creating:
                    if not self.store.todoist_begin_create(task['id']):
                        continue
                    remote_id = self.client.create_task(task, request_id=task['request_id'],
                                                        project_id=self.settings.todoist_project_id)
                    self.store.todoist_link(task['id'], remote_id)
                    creating = False
                    remote_completed = False
                else:
                    remote_id = task['remote_id']
                    remote_completed = self.client.task_completed(remote_id)
                if remote_completed:
                    self.store.todoist_remote_complete(task['id'])
                elif task['completed_at'] is not None:
                    close_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'mailbot:todoist:close:' + task['id']))
                    self.client.close_task(remote_id, request_id=close_id)
                    self.store.todoist_finish(task['id'], True)
                else:
                    self.store.todoist_finish(task['id'], False)
            except TodoistError as error:
                # Creation with an unknown result must never be blindly retried.
                code = 'REQUEST_FAILED' if error.ambiguous and not creating else error.code
                self.store.todoist_fail(task['id'], code, ambiguous=creating and error.ambiguous,
                                        retry_after=error.retry_after)
                self.last_error = error.code
                self.retry_after = error.retry_after
                self.state = 'AUTH_REQUIRED' if error.code == 'AUTH_REQUIRED' else 'ERROR'
                break
        if self.state != 'READY':
            return
        for linked in self.store.todoist_checks():
            try:
                if self.client.task_completed(linked['remote_id']):
                    self.store.todoist_remote_complete(linked['task_id'])
            except TodoistError as error:
                self.last_error = error.code
                self.retry_after = error.retry_after
                self.state = 'AUTH_REQUIRED' if error.code == 'AUTH_REQUIRED' else 'ERROR'
                if error.code in {'AUTH_REQUIRED', 'REMOTE_MISSING'}:
                    self.store.todoist_fail(linked['task_id'], error.code)
                break

    async def run(self):
        while True:
            # A cancelled to_thread operation can still be committing its receipt.
            # Wait for it before closing the database or starting another worker.
            operation = asyncio.create_task(asyncio.to_thread(self.cycle))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise
            except Exception:
                self.state, self.last_error = 'ERROR', 'SYNC_FAILED'
            await asyncio.sleep(max(self.settings.todoist_poll_seconds, self.retry_after))
