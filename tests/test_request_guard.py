"""Tests for the POST guard in server.py. Standard library only.

Run from the repo root: python3 -m unittest discover -s tests

HOME and CC_DATA point to a temp folder before the import, so nothing here can
read or change a real ~/.claude. No socket is opened: do_POST is called directly.
"""
import base64, hashlib, io, json, os, sys, tempfile, unittest
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.TemporaryDirectory()
os.environ["HOME"] = _tmp.name
os.environ["CC_DATA"] = _tmp.name
os.environ["CC_PORT"] = "7799"
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

HOST = "127.0.0.1:7799"
PAGE = "http://127.0.0.1:7799"
EXT = f"chrome-extension://{server.EXTENSION_ID}"


def extension_id_from_manifest():
    """Chrome's rule: sha256 of the public key, first 16 bytes, hex digits as a-p."""
    key = json.loads((ROOT / "extension" / "manifest.json").read_text())["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


class PostRefusal(unittest.TestCase):
    def test_allowed_pairs(self):
        for host, origin in [(HOST, PAGE),
                             ("localhost:7799", "http://localhost:7799"),
                             (HOST, EXT)]:
            with self.subTest(host=host, origin=origin):
                self.assertIsNone(server.post_refusal([host], [origin]))

    def test_refused_hosts(self):
        for host in ["evil.example:7799", "evil.example", "127.0.0.1", "127.0.0.1:80",
                     "127.0.0.1:7799.evil.example", "evil.example:7799@127.0.0.1:7799",
                     "0.0.0.0:7799", "[::1]:7799", "LOCALHOST.evil:7799", ""]:
            with self.subTest(host=host):
                self.assertEqual(server.post_refusal([host], [PAGE]), "host")

    def test_refused_origins(self):
        for origin in ["https://evil.example", "http://evil.example:7799", "null", "",
                       "http://127.0.0.1", "http://127.0.0.1:1234", "https://127.0.0.1:7799",
                       "http://127.0.0.1:7799.evil.example", "http://127.0.0.1:7799/",
                       "chrome-extension://" + "a" * 32, "chrome-extension://",
                       "moz-extension://" + server.EXTENSION_ID]:
            with self.subTest(origin=origin):
                self.assertEqual(server.post_refusal([HOST], [origin]), "origin")

    def test_missing_or_repeated_headers(self):
        self.assertEqual(server.post_refusal([], [PAGE]), "host")
        self.assertEqual(server.post_refusal([HOST], []), "origin")
        self.assertEqual(server.post_refusal([HOST, "evil.example"], [PAGE]), "host")
        self.assertEqual(server.post_refusal([HOST], [PAGE, "https://evil.example"]), "origin")

    def test_allowlist_follows_port(self):
        with mock.patch.object(server, "PORT", 8123):
            self.assertIsNone(server.post_refusal(["127.0.0.1:8123"], ["http://127.0.0.1:8123"]))
            self.assertEqual(server.post_refusal([HOST], [PAGE]), "host")


def call_post(path, headers, body=b'{"enabled": false}'):
    """Run H.do_POST on a bare handler, without a socket."""
    h = server.H.__new__(server.H)
    h.path = path
    h.request_version = "HTTP/1.1"
    h.requestline = "POST " + path + " HTTP/1.1"
    h.client_address = ("127.0.0.1", 1)
    h.headers = Message()
    for k, v in headers:
        h.headers[k] = v
    h.headers["Content-Length"] = str(len(body))
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.do_POST()
    raw = h.wfile.getvalue()
    status = int(raw.split(b" ", 2)[1])
    return status, json.loads(raw.split(b"\r\n\r\n", 1)[1]), h


class DoPost(unittest.TestCase):
    """/api/sounds with enabled=false creates a flag file: an easy side effect to see."""

    def setUp(self):
        server.SOUNDS_OFF_FLAG.unlink(missing_ok=True)
        p = mock.patch.object(server, "state", lambda: {})   # no tmux or disk scans
        p.start()
        self.addCleanup(p.stop)

    def test_page_is_served(self):
        status, _, _ = call_post("/api/sounds", [("Host", HOST), ("Origin", PAGE)])
        self.assertEqual(status, 200)
        self.assertTrue(server.SOUNDS_OFF_FLAG.exists())

    def test_extension_is_served(self):
        status, _, _ = call_post("/api/sounds", [("Host", HOST), ("Origin", EXT)])
        self.assertEqual(status, 200)
        self.assertTrue(server.SOUNDS_OFF_FLAG.exists())

    def test_refused_requests_change_nothing(self):
        for headers in ([("Host", HOST), ("Origin", "https://evil.example")],
                        [("Host", "evil.example:7799"), ("Origin", "http://evil.example:7799")],
                        [("Host", "evil.example:7799"), ("Origin", PAGE)],
                        [("Host", HOST)],
                        [("Origin", PAGE)],
                        [("Host", HOST), ("Host", "evil.example"), ("Origin", PAGE)],
                        []):
            with self.subTest(headers=headers):
                status, reply, h = call_post("/api/sounds", headers)
                self.assertEqual(status, 403)
                self.assertIn("error", reply)
                self.assertFalse(server.SOUNDS_OFF_FLAG.exists())
                self.assertEqual(h.rfile.tell(), 0)          # body was not read

    def test_refused_shutdown_does_not_stop_the_server(self):
        fake = mock.Mock()
        with mock.patch.object(server.H, "server_ref", fake):
            status, _, _ = call_post("/api/shutdown", [("Host", HOST), ("Origin", "https://evil.example")])
        self.assertEqual(status, 403)
        fake.shutdown.assert_not_called()

    def test_query_string_does_not_skip_the_check(self):
        status, _, _ = call_post("/api/sounds?lang=en", [("Host", HOST), ("Origin", "null")])
        self.assertEqual(status, 403)
        self.assertFalse(server.SOUNDS_OFF_FLAG.exists())


class Fixed(unittest.TestCase):
    def test_binds_to_loopback_only(self):
        self.assertEqual(server.BIND_HOST, "127.0.0.1")
        source = (ROOT / "server.py").read_text()
        self.assertIn("ThreadingHTTPServer((BIND_HOST, PORT), H)", source)

    def test_extension_id_matches_manifest_key(self):
        self.assertEqual(server.EXTENSION_ID, extension_id_from_manifest())

    def test_manifest_holds_no_private_key(self):
        text = (ROOT / "extension" / "manifest.json").read_text()
        self.assertNotIn("PRIVATE", text)
        # 2048-bit RSA public key in DER form is 294 bytes; a private key is far longer
        self.assertEqual(len(base64.b64decode(json.loads(text)["key"])), 294)


if __name__ == "__main__":
    unittest.main()
