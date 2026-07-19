import unittest

from app.server import request_client_ip


class ClientIpTests(unittest.TestCase):
    def test_uses_forwarded_address_from_local_proxy(self):
        self.assertEqual(request_client_ip("127.0.0.1", "203.0.113.10, 10.0.0.1"), "203.0.113.10")

    def test_ignores_forwarded_address_from_remote_peer(self):
        self.assertEqual(request_client_ip("198.51.100.20", "203.0.113.10"), "198.51.100.20")

    def test_ignores_invalid_forwarded_address(self):
        self.assertEqual(request_client_ip("::1", "not-an-ip"), "::1")
