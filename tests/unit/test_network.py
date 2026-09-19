import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.feishu.credentials import LocalConfigStore
from cfr.network import _parse_wininet_proxy_server, proxy_child_env, resolve_cfr_proxy, resolve_proxy, sanitized_proxy_url
import tempfile


class NetworkTests(unittest.TestCase):
    def test_environment_proxy_has_priority_over_system_proxy(self):
        resolved = resolve_proxy(environment={'HTTPS_PROXY': 'http://env.example:8080'}, system_proxies={'https': 'http://system.example:8080'})
        self.assertEqual(resolved.source, 'environment')
        self.assertEqual(resolved.https_proxy, 'http://env.example:8080')

    def test_system_proxy_fallback_and_child_no_proxy(self):
        resolved = resolve_proxy(environment={}, system_proxies={'http': 'http://127.0.0.1:57777', 'https': 'http://127.0.0.1:57777'})
        child = proxy_child_env(resolved)
        self.assertEqual(resolved.source, 'system')
        self.assertEqual(child['HTTPS_PROXY'], 'http://127.0.0.1:57777')
        self.assertIn('localhost', child['NO_PROXY'])
        self.assertEqual(child['ALL_PROXY'], '')

    def test_direct_child_environment_clears_all_inherited_proxy_variables(self):
        child = proxy_child_env(resolve_proxy(environment={}, system_proxies={}))
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            self.assertEqual(child[name], '')
        self.assertIn('127.0.0.1', child['NO_PROXY'])

    def test_direct_fallback_and_credential_redaction(self):
        resolved = resolve_proxy(environment={}, system_proxies={})
        self.assertEqual(resolved.mode, 'direct')
        self.assertEqual(sanitized_proxy_url('http://user:secret@host.example:80/path'), 'http://host.example:80')

    def test_wininet_proxy_fallback_when_urllib_does_not_expose_windows_proxy(self):
        with patch('cfr.network.urllib.request.getproxies', return_value={'devspace_trust': '1'}), patch(
            'cfr.network._wininet_proxies',
            return_value={'http': 'http://127.0.0.1:57777', 'https': 'http://127.0.0.1:57777'},
        ):
            resolved = resolve_proxy(environment={})
        self.assertEqual(resolved.source, 'wininet')
        self.assertEqual(resolved.https_proxy, 'http://127.0.0.1:57777')

    def test_wininet_proxy_server_parser_supports_common_windows_forms(self):
        self.assertEqual(
            _parse_wininet_proxy_server('127.0.0.1:57777'),
            {'http': 'http://127.0.0.1:57777', 'https': 'http://127.0.0.1:57777'},
        )

    def test_persistent_direct_and_proxy_policy_apply_without_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalConfigStore(directory)
            store.set_network_policy('direct')
            direct = resolve_cfr_proxy(environment={}, system_proxies={'https': 'http://system.example:8080'}, config_store=store)
            self.assertEqual((direct.mode, direct.source), ('direct', 'persistent'))
            store.set_network_policy('proxy', 'http://127.0.0.1:7890')
            proxy = resolve_cfr_proxy(environment={}, system_proxies={}, config_store=store)
            self.assertEqual((proxy.mode, proxy.source, proxy.https_proxy), ('proxy', 'persistent', 'http://127.0.0.1:7890'))

    def test_explicit_direct_policy_overrides_inherited_environment_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalConfigStore(directory)
            store.set_network_policy('direct')
            result = resolve_cfr_proxy(environment={'HTTPS_PROXY': 'http://env.example:8080'}, system_proxies={}, config_store=store)
            self.assertEqual((result.mode, result.source, result.https_proxy), ('direct', 'persistent', None))
        self.assertEqual(
            _parse_wininet_proxy_server('http=127.0.0.1:8080;https=127.0.0.1:8443;socks=127.0.0.1:1080'),
            {'http': 'http://127.0.0.1:8080', 'https': 'http://127.0.0.1:8443'},
        )


if __name__ == '__main__':
    unittest.main()
