import unittest

from app.server import Handler, request_client_ip


class ClientIpTests(unittest.TestCase):
    def test_uses_forwarded_address_from_local_proxy(self):
        self.assertEqual(request_client_ip("127.0.0.1", "203.0.113.10, 10.0.0.1"), "203.0.113.10")

    def test_ignores_forwarded_address_from_remote_peer(self):
        self.assertEqual(request_client_ip("198.51.100.20", "203.0.113.10"), "198.51.100.20")

    def test_ignores_invalid_forwarded_address(self):
        self.assertEqual(request_client_ip("::1", "not-an-ip"), "::1")

    def test_invalid_session_is_rate_limited_by_ip(self):
        handler = object.__new__(Handler)
        handler._cached_user = None
        handler._client_ip = lambda: "203.0.113.20"
        self.assertEqual(handler._rate_key(), "ip:203.0.113.20")

    def test_valid_session_is_rate_limited_by_user(self):
        handler = object.__new__(Handler)
        handler._cached_user = {"open_id": "ou_rate_user"}
        handler._client_ip = lambda: "203.0.113.20"
        self.assertEqual(handler._rate_key(), "user:ou_rate_user")
