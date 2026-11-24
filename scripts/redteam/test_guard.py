"""The red-team harness must never leave the machine (MIP-2267).

python -m unittest scripts/redteam/test_guard.py   (standard library only)
"""

import http.server
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from guard import LocalClient, NotLocal, check_local  # noqa: E402


class CheckLocalTests(unittest.TestCase):
    def test_local_hosts_pass(self):
        for url in (
            "http://127.0.0.1:18267/api/v2/",
            "http://localhost:8000/",
            "https://localhost/x",
            "http://[::1]:9000/",
        ):
            self.assertEqual(check_local(url), url)

    def test_public_and_lookalike_hosts_refused(self):
        for url in (
            "https://genosai.dev/",
            "https://api.genosai.dev/api/v2/",
            "https://backend-collab-production.up.railway.app/",
            "http://127.0.0.1.nip.io/",
            "http://localhost.example.com/",
            "http://127.0.0.2/",
            "http://0x7f.0.0.1/",
            "http://2130706433/",
            "http://0.0.0.0/",
        ):
            with self.assertRaises(NotLocal, msg=url):
                check_local(url)

    def test_credentials_schemes_and_junk_refused(self):
        for url in (
            "http://user:pw@127.0.0.1/",
            "http://evil.example@127.0.0.1/",
            "ftp://127.0.0.1/",
            "file:///etc/passwd",
            "gopher://localhost/",
            "",
            "http://127.0.0.1:99999/",
        ):
            with self.assertRaises(NotLocal, msg=url):
                check_local(url)

    def test_client_refuses_public_base_and_absolute_paths(self):
        with self.assertRaises(NotLocal):
            LocalClient("https://api.genosai.dev")
        client = LocalClient("http://127.0.0.1:1")
        with self.assertRaises(NotLocal):
            client.request("GET", "https://api.genosai.dev/api/v2/")


class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "https://genosai.dev/")
        self.end_headers()

    def log_message(self, *args):
        pass


class RedirectTests(unittest.TestCase):
    def test_redirect_to_a_public_host_is_not_followed(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LocalClient(f"http://127.0.0.1:{server.server_address[1]}")
            res = client.request("GET", "/")
            self.assertEqual(res.status, 302)
            self.assertEqual(res.headers.get("Location"), "https://genosai.dev/")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
