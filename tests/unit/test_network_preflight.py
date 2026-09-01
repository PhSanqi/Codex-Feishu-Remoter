import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))

from cfr.network import sanitized_proxy_url
from run_codex_network_preflight import classify_transport


class NetworkPreflightTests(unittest.TestCase):
    def test_proxy_redaction_keeps_only_scheme_host_port(self):
        self.assertEqual(sanitized_proxy_url('https://user:secret@example.test:8443/path?token=hidden'), 'https://example.test:8443')
        self.assertIsNone(sanitized_proxy_url(None))

    def test_transport_classification(self):
        self.assertEqual(classify_transport('stream disconnected before completion: error sending request for url (https://api.openai.com/v1/responses)'), 'CODEX_MODEL_NETWORK_FAILURE')
        self.assertEqual(classify_transport('401 unauthorized'), 'CODEX_AUTH_FAILURE')


if __name__ == '__main__':
    unittest.main()
