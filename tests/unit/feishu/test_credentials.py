import json
from pathlib import Path
import tempfile
import unittest

from cfr.core.models import StructuredError
from cfr.feishu.credentials import (
    FeishuCredentialResolver,
    KEYRING_SECRET_KEY,
    LocalConfigStore,
    clear_persistent_all,
    import_environment_credentials,
)
from cfr.feishu.config import load_settings


class FakeSecretStore:
    available = True

    def __init__(self):
        self.values = {}

    def set_secret(self, key, value):
        self.values[key] = value

    def get_secret(self, key):
        return self.values.get(key)

    def delete_secret(self, key):
        self.values.pop(key, None)

    def has_secret(self, key):
        return bool(self.values.get(key))


class UnavailableSecretStore:
    available = False

    def set_secret(self, *_args):
        raise StructuredError('CFR_SECURE_SECRET_STORE_UNAVAILABLE', 'unavailable')

    def get_secret(self, *_args):
        raise StructuredError('CFR_SECURE_SECRET_STORE_UNAVAILABLE', 'unavailable')

    def delete_secret(self, *_args):
        raise StructuredError('CFR_SECURE_SECRET_STORE_UNAVAILABLE', 'unavailable')

    def has_secret(self, *_args):
        return False


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = LocalConfigStore(Path(self.temp.name))
        self.secrets = FakeSecretStore()
        self.secrets.set_secret(KEYRING_SECRET_KEY, 'persistent-secret')
        self.config.set_app_id('persistent-app')

    def tearDown(self):
        self.temp.cleanup()

    def test_env_credentials_override_persistent(self):
        result = FeishuCredentialResolver({'CFR_FEISHU_APP_ID': 'env-app', 'CFR_FEISHU_APP_SECRET': 'env-secret'}, self.config, self.secrets).resolve()
        self.assertEqual((result.app_id, result.app_secret), ('env-app', 'env-secret'))
        self.assertEqual((result.app_id_source, result.app_secret_source), ('environment', 'environment'))

    def test_persistent_credentials_used_when_env_missing(self):
        result = FeishuCredentialResolver({}, self.config, self.secrets).resolve()
        self.assertTrue(result.configured)
        self.assertEqual((result.app_id_source, result.app_secret_source), ('persistent', 'persistent'))

    def test_missing_credentials_fail_closed(self):
        result = FeishuCredentialResolver({}, LocalConfigStore(Path(self.temp.name) / 'missing'), FakeSecretStore()).resolve()
        self.assertFalse(result.configured)
        self.assertEqual((result.app_id_source, result.app_secret_source), ('missing', 'missing'))

    def test_app_id_atomic_write(self):
        self.config.set_app_id('new-app')
        self.assertEqual(json.loads(self.config.path.read_text())['feishu']['app_id'], 'new-app')
        self.assertFalse(any(path.name.startswith('.config.') for path in Path(self.temp.name).iterdir()))

    def test_secret_store_set_get_delete_fake_backend(self):
        self.secrets.set_secret('x', 'y')
        self.assertTrue(self.secrets.has_secret('x'))
        self.assertEqual(self.secrets.get_secret('x'), 'y')
        self.secrets.delete_secret('x')
        self.assertFalse(self.secrets.has_secret('x'))

    def test_import_env_persists_both(self):
        result = import_environment_credentials({'CFR_FEISHU_APP_ID': 'imported-app', 'CFR_FEISHU_APP_SECRET': 'imported-secret'}, self.config, self.secrets)
        self.assertTrue(result['AppIdConfigured'] and result['AppSecretConfigured'])
        resolved = FeishuCredentialResolver({}, self.config, self.secrets).resolve()
        self.assertEqual((resolved.app_id, resolved.app_secret), ('imported-app', 'imported-secret'))

    def test_import_env_never_echoes_secret(self):
        result = import_environment_credentials({'CFR_FEISHU_APP_ID': 'app', 'CFR_FEISHU_APP_SECRET': 'do-not-print'}, self.config, self.secrets)
        self.assertNotIn('do-not-print', json.dumps(result))

    def test_status_never_returns_secret(self):
        result = FeishuCredentialResolver({}, self.config, self.secrets).safe_status()
        self.assertNotIn('persistent-secret', json.dumps(result))
        self.assertIn('AppSecretConfigured', result)

    def test_keyring_unavailable_no_plaintext_fallback(self):
        config = LocalConfigStore(Path(self.temp.name) / 'unavailable')
        with self.assertRaises(StructuredError) as context:
            import_environment_credentials({'CFR_FEISHU_APP_ID': 'app', 'CFR_FEISHU_APP_SECRET': 'secret'}, config, UnavailableSecretStore())
        self.assertEqual(context.exception.code, 'CFR_SECURE_SECRET_STORE_UNAVAILABLE')
        self.assertFalse(config.path.exists())

    def test_persistent_security_policy_is_used_without_environment(self):
        root = Path(self.temp.name).resolve()
        self.config.set_feishu_security(['ou-paired'], [str(root)])
        settings = load_settings({}, config_store=self.config)
        self.assertEqual(settings.allowed_open_ids, ('ou-paired',))
        self.assertEqual(settings.allowed_workspace_roots, (root,))

    def test_environment_security_policy_overrides_persistent_values(self):
        root = Path(self.temp.name).resolve()
        self.config.set_feishu_security(['ou-paired'], [str(root)])
        settings = load_settings({
            'CFR_FEISHU_ALLOWED_OPEN_IDS': 'ou-env',
            'CFR_FEISHU_ALLOWED_WORKSPACE_ROOTS': str(root / 'env-root'),
        }, config_store=self.config)
        self.assertEqual(settings.allowed_open_ids, ('ou-env',))
        self.assertEqual(settings.allowed_workspace_roots, (root / 'env-root',))

    def test_loading_an_invalid_workspace_root_does_not_mutate_persistent_policy(self):
        missing = Path(self.temp.name) / 'missing-root'
        self.config.set_feishu_security(['ou-paired'], [str(missing)])
        before = self.config.path.read_text(encoding='utf-8')
        settings = load_settings({}, config_store=self.config)
        self.assertEqual(settings.allowed_workspace_roots, (missing,))
        self.assertEqual(self.config.path.read_text(encoding='utf-8'), before)

    def test_security_policy_write_never_writes_secret(self):
        self.config.set_feishu_security(['ou-paired'], [self.temp.name])
        value = json.loads(self.config.path.read_text())
        self.assertEqual(value['feishu']['allowed_open_ids'], ['ou-paired'])
        self.assertNotIn('app_secret', json.dumps(value))

    def test_credential_clear_preserves_security_policy(self):
        self.config.set_feishu_security(['ou-paired'], [self.temp.name])
        clear_persistent_all(self.config, self.secrets)
        value = json.loads(self.config.path.read_text())
        self.assertEqual(value['feishu']['allowed_open_ids'], ['ou-paired'])
        self.assertEqual(value['feishu']['allowed_workspace_roots'], [self.temp.name])
        self.assertNotIn('app_id', value['feishu'])


if __name__ == '__main__':
    unittest.main()
