import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.diagnostics import routing_config_files
from cfr.config import child_process_env, resolve_cfr_codex_home
from cfr.storage.db import BindingStore


class BoundaryContractTests(unittest.TestCase):
    def test_diagnostics_reads_config_from_resolved_cfr_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfr_home = root / 'cfr-home'
            cfr_home.mkdir()
            (cfr_home / 'config.toml').write_text('model_provider = "chatgpt"\n', encoding='utf-8')
            files = routing_config_files(root, codex_home=cfr_home, environment={'CODEX_HOME': str(root / 'ambient')})
            self.assertEqual(files[0]['path'], str(cfr_home / 'config.toml'))

    def test_resolved_home_owns_child_environment(self):
        resolved = resolve_cfr_codex_home('C:/cfr', {'CFR_CODEX_HOME': 'C:/env', 'CODEX_HOME': 'C:/ambient'})
        child = child_process_env({'CODEX_HOME': 'C:/ordinary'}, resolved)
        self.assertEqual(child['CODEX_HOME'], str(resolved.path))

    def test_runtime_lease_schema_belongs_to_cfr_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            store = BindingStore(database)
            connection = sqlite3.connect(database)
            try:
                rows = connection.execute("select name from sqlite_master where type='table' and name='cfr_thread_runtime_leases'").fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [('cfr_thread_runtime_leases',)])
            store.close()

    def test_pinned_protocol_sha_is_unchanged(self):
        path = Path(__file__).resolve().parents[2] / 'docs' / 'reference' / 'CFR_AND_BROKER_COEXISTENCE_ROUTING_CONTRACT_v1.2.md'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(digest, '9f7d498cb510be38baa10422b46860b2a4599898affe2ae56cdc73460f90908b')


if __name__ == '__main__':
    unittest.main()
