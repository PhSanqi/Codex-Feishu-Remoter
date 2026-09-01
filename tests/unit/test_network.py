import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.network import _parse_wininet_proxy_server, proxy_child_env, resolve_proxy, sanitized_proxy_url


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
        self.assertNotIn('ALL_PROXY', child)

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
        self.assertEqual(
            _parse_wininet_proxy_server('http=127.0.0.1:8080;https=127.0.0.1:8443;socks=127.0.0.1:1080'),
            {'http': 'http://127.0.0.1:8080', 'https': 'http://127.0.0.1:8443'},
        )


if __name__ == '__main__':
    unittest.main()
