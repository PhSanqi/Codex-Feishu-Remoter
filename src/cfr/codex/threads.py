from pathlib import Path

from cfr.core.models import ThreadRef


class ThreadManager:
    def __init__(self, client):
        self.client = client
        self.last_create_result = None

    @staticmethod
    def _thread(result):
        return (result or {}).get('thread') or result or {}

    @classmethod
    def ref_from_result(cls, result, fallback_cwd=None, fallback_name=None):
        thread = cls._thread(result)
        thread_id = thread.get('id') or (result or {}).get('threadId')
        if not thread_id:
            raise RuntimeError('Codex response returned no thread id')
        cwd = Path(thread.get('cwd') or fallback_cwd or '.')
        path = Path(thread['path']) if thread.get('path') else None
        return ThreadRef(thread_id, thread.get('name') or fallback_name, cwd, path)

    def create_thread(self, cwd, name=None):
        result = self.client.request('thread/start', {
            'cwd': str(cwd),
            'ephemeral': False,
            'threadSource': 'user',
            'historyMode': 'legacy',
            'sessionStartSource': 'startup',
        })
        self.last_create_result = result
        return self.ref_from_result(result, cwd, name)

    def name_thread(self, thread_id, name):
        return self.client.request('thread/name/set', {'threadId': thread_id, 'name': name})

    def resume_thread(self, thread_id):
        return self.client.request('thread/resume', {'threadId': thread_id})

    def read_thread(self, thread_id):
        return self.client.request('thread/read', {'threadId': thread_id, 'includeTurns': True})

    def list_threads(self):
        return self.client.request('thread/list', {})
