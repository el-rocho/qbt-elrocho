"""Contrato de sesión, errores y capacidades del cliente WebUI."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from qbt.qbittorrent import (
    QBittorrentAuthError,
    QBittorrentClient,
    QBittorrentError,
    ServerCapabilities,
)

TOKEN = "sesion-token-123"
SID_COOKIE = f"QBT_SID_8080={TOKEN}"


class _Handler(BaseHTTPRequestHandler):
    """Servidor estricto: solo responde (método, ruta exacta) registrados."""

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {
            "method": self.command,
            "path": self.path,
            "body": body,
            "cookie": self.headers.get("Cookie"),
            "referer": self.headers.get("Referer"),
            "origin": self.headers.get("Origin"),
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
        }
        self.server.requests.append(record)

        route = self.server.routes.get((self.command, self.path))
        if route is None:
            self._send(404, b"", {})
            return
        if callable(route):
            status, payload, headers = route(record)
        else:
            status, payload, headers = route
        self._send(status, payload, headers)

    def _send(self, status, payload, headers):
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "text/plain; charset=UTF-8")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class _ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.routes = {}
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.origin = self.base_url

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def route(self, method, path, status=200, body=b"", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.server.routes[(method, path)] = (status, body, headers or {})

    def login_route(self, cookie_name="QBT_SID_8080", token=TOKEN, status=200, body=b"Ok."):
        return (
            "POST",
            "/api/v2/auth/login",
            status,
            body,
            {"Set-Cookie": f"{cookie_name}={token}; path=/"},
        )

    def add_login_route(self, cookie_name="QBT_SID_8080", token=TOKEN, status=200, body=b"Ok."):
        method, path, status, body, headers = self.login_route(cookie_name, token, status, body)
        self.route(method, path, status, body, headers)

    def protect(self, path, cookie=SID_COOKIE, body=b"[]", reject_status=401):
        expected = cookie

        def responder(record):
            if expected is None:
                return 200, body, {}
            if record["cookie"] != expected:
                return reject_status, b"", {}
            return 200, body, {}

        self.server.routes[("GET", path)] = responder

    def paths(self):
        return [request["path"] for request in self.server.requests]


class LoginTests(_ServerTestCase):
    def test_login_accepts_204_with_cookie_and_reuses_session(self):
        self.add_login_route(status=204, body=b"")
        self.protect("/api/v2/app/version", body=b"v5.3.0")
        client = QBittorrentClient(self.base_url)
        self.assertIsNone(client.login("admin", "clave"))
        self.assertTrue(client.authenticated)
        self.assertEqual(client.version(), "v5.3.0")
        self.assertEqual(self.server.requests[-1]["cookie"], SID_COOKIE)

    def test_login_204_requires_usable_cookie(self):
        for cookie in (None, f"{SID_COOKIE}; Path=/other/", f"{SID_COOKIE}; Path=/; Secure"):
            with self.subTest(cookie=cookie):
                headers = {"Set-Cookie": cookie} if cookie else {}
                self.route("POST", "/api/v2/auth/login", 204, b"", headers)
                client = QBittorrentClient(self.base_url)
                with self.assertRaises(QBittorrentAuthError):
                    client.login("admin", "clave")
                self.assertFalse(client.authenticated)
                self.route("GET", "/api/v2/app/version", 200, b"v5.3.0")
                client.version()
                self.assertIsNone(self.server.requests[-1]["cookie"])

    def test_unexpected_success_status_is_not_a_login(self):
        for status in (201, 202, 206):
            with self.subTest(status=status):
                self.add_login_route(status=status)
                client = QBittorrentClient(self.base_url)
                with self.assertRaises(QBittorrentAuthError):
                    client.login("admin", "clave")
                self.assertFalse(client.authenticated)
                self.route("GET", "/api/v2/app/version", 200, b"v5.3.0")
                client.version()
                self.assertIsNone(self.server.requests[-1]["cookie"])

    def test_oversized_login_response_discards_cookie(self):
        self.add_login_route(body=b"Ok." + b" " * 80)
        client = QBittorrentClient(self.base_url, max_response_bytes=32)
        with self.assertRaises(QBittorrentError):
            client.login("admin", "clave")
        self.route("GET", "/api/v2/app/version", 200, b"v5.0.0")
        client.version()
        self.assertIsNone(self.server.requests[-1]["cookie"])
        self.assertFalse(client.authenticated)

    def test_login_requires_cookie_usable_for_api(self):
        for attributes in ("Path=/other/", "Path=/; Secure", "Path=/; Max-Age=0"):
            with self.subTest(attributes=attributes):
                self.route("POST", "/api/v2/auth/login", 200, b"Ok.",
                           {"Set-Cookie": f"SID={TOKEN}; {attributes}"})
                client = QBittorrentClient(self.base_url)
                with self.assertRaises(QBittorrentAuthError):
                    client.login("admin", "clave")
                self.assertFalse(client.authenticated)

    def test_login_posts_exact_form_with_referer_and_origin(self):
        self.add_login_route()
        client = QBittorrentClient(self.base_url)
        self.assertIsNone(client.login("admin", "s3cr&t/plus"))
        self.assertTrue(client.authenticated)

        request = self.server.requests[-1]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/api/v2/auth/login")
        self.assertEqual(request["body"], b"username=admin&password=s3cr%26t%2Fplus")
        self.assertEqual(request["content_type"], "application/x-www-form-urlencoded")
        self.assertEqual(request["origin"], self.origin)
        self.assertIn(request["referer"], (self.base_url, self.base_url + "/"))
        self.assertEqual(urlsplit(request["referer"]).netloc, urlsplit(self.origin).netloc)
        self.assertIsNone(request["authorization"])

    def test_login_does_not_send_api_key_to_auth_endpoint(self):
        self.add_login_route()
        client = QBittorrentClient(self.base_url, api_key="qbt_api_key")
        client.login("admin", "clave")
        self.assertIsNone(self.server.requests[-1]["authorization"])

    def test_login_accepts_ok_with_trailing_newline(self):
        self.add_login_route(body=b"Ok.\n")
        client = QBittorrentClient(self.base_url)
        client.login("admin", "clave")
        self.assertTrue(client.authenticated)

    def test_login_rejects_any_response_other_than_ok(self):
        for body in (b"Fails.", b"", b"Ok", b"ok.", b"OK."):
            with self.subTest(body=body):
                self.add_login_route(body=body)
                client = QBittorrentClient(self.base_url)
                with self.assertRaises(QBittorrentAuthError):
                    client.login("admin", "clave")
                self.assertFalse(client.authenticated)

    def test_login_fails_does_not_keep_a_session(self):
        self.add_login_route(body=b"Fails.")
        client = QBittorrentClient(self.base_url)
        with self.assertRaises(QBittorrentAuthError):
            client.login("admin", "clave")
        self.assertFalse(client.authenticated)

        self.route("GET", "/api/v2/app/version", 200, b"v5.0.0")
        self.assertEqual(client.version(), "v5.0.0")
        self.assertIsNone(self.server.requests[-1]["cookie"])

    def test_login_401_is_auth_error_with_status(self):
        self.add_login_route(status=401, body=b"")
        client = QBittorrentClient(self.base_url)
        with self.assertRaises(QBittorrentAuthError) as caught:
            client.login("admin", "clave")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(client.authenticated)

    def test_login_403_is_auth_error_with_status(self):
        self.add_login_route(status=403, body=b"")
        client = QBittorrentClient(self.base_url)
        with self.assertRaises(QBittorrentAuthError) as caught:
            client.login("admin", "clave")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("403", str(caught.exception))
        self.assertFalse(client.authenticated)

    def test_login_ok_without_cookie_is_rejected(self):
        self.route("POST", "/api/v2/auth/login", 200, b"Ok.")
        client = QBittorrentClient(self.base_url)
        with self.assertRaises(QBittorrentAuthError):
            client.login("admin", "clave")
        # Sin Set-Cookie no hay sesión utilizable: no se puede afirmar autenticado.
        self.assertFalse(client.authenticated)
        self.route("GET", "/api/v2/app/version", 401, b"")
        with self.assertRaises(QBittorrentAuthError):
            client.version()

    def test_uses_cookie_name_returned_by_server(self):
        for name in ("QBT_SID_8080", "SID", "ALGO_RARO"):
            with self.subTest(cookie_name=name):
                self.server.requests.clear()
                self.add_login_route(cookie_name=name)
                client = QBittorrentClient(self.base_url)
                client.login("admin", "clave")

                self.protect("/api/v2/app/version", cookie=f"{name}={TOKEN}", body=b"v5.0.0")
                self.assertEqual(client.version(), "v5.0.0")
                self.assertEqual(self.server.requests[-1]["cookie"], f"{name}={TOKEN}")


class SessionTests(_ServerTestCase):
    def test_empty_204_is_still_invalid_for_data_endpoints(self):
        for endpoint, operation in (
            ("/api/v2/app/version", lambda client: client.version()),
            ("/api/v2/torrents/info?includeFiles=false", lambda client: client.torrents_info()),
        ):
            with self.subTest(endpoint=endpoint):
                self.route("GET", endpoint, 204, b"")
                with self.assertRaisesRegex(QBittorrentError, "vacía"):
                    operation(QBittorrentClient(self.base_url))

    def test_unauthenticated_request_raises_auth_error_and_no_cookie(self):
        self.route("GET", "/api/v2/app/version", 401, b"")
        client = QBittorrentClient(self.base_url)
        with self.assertRaises(QBittorrentAuthError) as caught:
            client.version()
        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(client.authenticated)
        self.assertIsNone(self.server.requests[-1]["cookie"])

    def test_expired_session_is_cleared_on_401(self):
        self._assert_session_expiry_clears(401)

    def test_expired_session_is_cleared_on_403(self):
        self._assert_session_expiry_clears(403)

    def _assert_session_expiry_clears(self, status):
        self.add_login_route()
        self.route("GET", "/api/v2/app/version", 200, b"v5.0.0")
        client = QBittorrentClient(self.base_url)
        client.login("admin", "clave")
        self.assertTrue(client.authenticated)
        self.assertEqual(client.version(), "v5.0.0")
        self.assertEqual(self.server.requests[-1]["cookie"], SID_COOKIE)

        self.route("GET", "/api/v2/app/version", status, b"")
        with self.assertRaises(QBittorrentAuthError) as caught:
            client.version()
        self.assertEqual(caught.exception.status_code, status)
        self.assertFalse(client.authenticated)

        self.route("GET", "/api/v2/app/version", 200, b"v5.0.0")
        self.assertEqual(client.version(), "v5.0.0")
        self.assertIsNone(self.server.requests[-1]["cookie"])

    def test_clear_session_is_local_and_removes_cookie(self):
        self.add_login_route()
        client = QBittorrentClient(self.base_url)
        client.login("admin", "clave")
        requests_before = len(self.server.requests)

        client.clear_session()

        self.assertEqual(len(self.server.requests), requests_before)
        self.assertFalse(client.authenticated)
        self.route("GET", "/api/v2/app/version", 200, b"v5.0.0")
        self.assertEqual(client.version(), "v5.0.0")
        self.assertIsNone(self.server.requests[-1]["cookie"])

    def test_clients_do_not_share_sessions(self):
        self.add_login_route()
        self.protect("/api/v2/app/version", body=b"v5.0.0")
        first = QBittorrentClient(self.base_url)
        second = QBittorrentClient(self.base_url)
        first.login("admin", "clave")

        with self.assertRaises(QBittorrentAuthError):
            second.version()
        self.assertIsNone(self.server.requests[-1]["cookie"])
        self.assertFalse(second.authenticated)
        self.assertTrue(first.authenticated)
        self.assertEqual(first.version(), "v5.0.0")


class RedirectTests(_ServerTestCase):
    def test_login_redirect_is_rejected_without_following(self):
        self.route("POST", "/api/v2/auth/login", 302, b"", {"Location": "/robo"})
        self.route("GET", "/robo", 200, b"robado")
        client = QBittorrentClient(self.base_url)

        with self.assertRaises(QBittorrentError) as caught:
            client.login("admin", "clave-secreta")
        self.assertEqual(caught.exception.status_code, 302)
        self.assertEqual(self.paths(), ["/api/v2/auth/login"])
        self.assertNotIn("clave-secreta", str(caught.exception))
        self.assertFalse(client.authenticated)

    def test_get_redirect_is_rejected_without_following(self):
        self.route("GET", "/api/v2/app/version", 301, b"", {"Location": "/otro"})
        self.route("GET", "/otro", 200, b"v9.9.9")
        client = QBittorrentClient(self.base_url)

        with self.assertRaises(QBittorrentError) as caught:
            client.version()
        self.assertEqual(caught.exception.status_code, 301)
        self.assertEqual(self.paths(), ["/api/v2/app/version"])


class ErrorContractTests(_ServerTestCase):
    def test_auth_error_is_a_qbittorrent_error(self):
        self.assertTrue(issubclass(QBittorrentAuthError, QBittorrentError))

    def test_errors_do_not_leak_password_api_key_or_cookie(self):
        self.add_login_route(status=401, body=b"")
        rejected = QBittorrentClient(self.base_url, api_key="qbt_api_secreto")
        with self.assertRaises(QBittorrentAuthError) as caught:
            rejected.login("admin", "password-secreta")
        for error in (caught.exception, caught.exception.args):
            self.assertNotIn("password-secreta", repr(error))
            self.assertNotIn("qbt_api_secreto", repr(error))

        self.add_login_route()
        expired = QBittorrentClient(self.base_url)
        expired.login("admin", "clave")
        self.route("GET", "/api/v2/app/version", 403, b"")
        with self.assertRaises(QBittorrentAuthError) as caught:
            expired.version()
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, repr(caught.exception.args))

    def test_transport_error_has_no_status_code(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = QBittorrentClient(f"http://127.0.0.1:{port}", timeout=2.0)
        with self.assertRaises(QBittorrentError) as caught:
            client.version()
        self.assertIsNone(caught.exception.status_code)
        self.assertFalse(isinstance(caught.exception, QBittorrentAuthError))

    def test_http_error_status_is_recorded(self):
        self.route("GET", "/api/v2/app/version", 500, b"")
        client = QBittorrentClient(self.base_url)
        with self.assertRaises(QBittorrentError) as caught:
            client.version()
        self.assertEqual(caught.exception.status_code, 500)

    def test_api_key_keeps_working_without_session(self):
        self.route("GET", "/api/v2/app/version", 403, b"")
        client = QBittorrentClient(self.base_url, api_key="qbt_api_key")
        with self.assertRaises(QBittorrentAuthError) as caught:
            client.version()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.server.requests[-1]["authorization"], "Bearer qbt_api_key")
        self.assertFalse(client.authenticated)


class PrefixTests(_ServerTestCase):
    def test_base_url_with_path_prefix(self):
        prefix = self.base_url + "/qbt"
        self.route(
            "POST", "/qbt/api/v2/auth/login", 200, b"Ok.",
            {"Set-Cookie": f"{SID_COOKIE}; path=/"},
        )
        self.protect("/qbt/api/v2/app/version", body=b"v5.0.0")
        client = QBittorrentClient(prefix)

        client.login("admin", "clave")
        request = self.server.requests[-1]
        self.assertEqual(request["origin"], self.origin)
        self.assertEqual(urlsplit(request["referer"]).netloc, urlsplit(self.origin).netloc)
        self.assertEqual(client.version(), "v5.0.0")
        self.assertEqual(self.server.requests[-1]["cookie"], SID_COOKIE)

    def test_base_url_with_trailing_slash(self):
        self.route("POST", "/api/v2/auth/login", 200, b"Ok.",
                   {"Set-Cookie": f"{SID_COOKIE}; Path=/"})
        client = QBittorrentClient(self.base_url + "/")
        client.login("admin", "clave")
        request = self.server.requests[-1]
        self.assertEqual(request["path"], "/api/v2/auth/login")
        self.assertEqual(request["referer"], self.base_url + "/")


class CapabilitiesTests(_ServerTestCase):
    def capability_routes(self, app_version=b"v5.0.0", webapi_version=b"2.11.8"):
        self.route("GET", "/api/v2/app/version", 200, app_version)
        self.route("GET", "/api/v2/app/webapiVersion", 200, webapi_version)

    def capabilities_for(self, app_version=b"v5.0.0", webapi_version=b"2.11.8"):
        self.capability_routes(app_version, webapi_version)
        return QBittorrentClient(self.base_url).capabilities()

    def test_returns_dataclass_with_versions_and_flags(self):
        capabilities = self.capabilities_for()
        self.assertIsInstance(capabilities, ServerCapabilities)
        self.assertEqual(capabilities.version, "v5.0.0")
        self.assertEqual(capabilities.webapi_version, "2.11.8")
        self.assertIs(capabilities.torrents, True)
        self.assertIs(capabilities.directories, True)

    def test_metadata_requires_webapi_2_11_8_or_newer(self):
        cases = {
            b"2.11.8": True,
            b"2.12.0": True,
            b"2.11.9": True,
            b"2.11.7": False,
            b"2.10.99": False,
            b"2.11": False,
        }
        for webapi_version, expected in cases.items():
            with self.subTest(webapi_version=webapi_version):
                capabilities = self.capabilities_for(webapi_version=webapi_version)
                self.assertIs(capabilities.directories, expected)
                self.assertIs(capabilities.torrents, True)

    def test_metadata_follows_webapi_not_app_version(self):
        newer_app = self.capabilities_for(app_version=b"v5.0.0", webapi_version=b"2.11.7")
        self.assertIs(newer_app.directories, False)
        older_app = self.capabilities_for(app_version=b"v4.6.0", webapi_version=b"2.11.8")
        self.assertIs(older_app.directories, True)

    def test_non_v2_webapi_claims_no_endpoint_support(self):
        for webapi_version in (b"1.9", b"3.0"):
            with self.subTest(webapi_version=webapi_version):
                capabilities = self.capabilities_for(webapi_version=webapi_version)
                self.assertIs(capabilities.torrents, False)
                self.assertIs(capabilities.directories, False)

    def test_capabilities_only_read_versions_not_permissions(self):
        self.capability_routes()
        QBittorrentClient(self.base_url).capabilities()
        self.assertEqual(
            self.paths(),
            ["/api/v2/app/version", "/api/v2/app/webapiVersion"],
        )

    def test_capabilities_propagate_auth_errors(self):
        self.route("GET", "/api/v2/app/version", 401, b"")
        self.route("GET", "/api/v2/app/webapiVersion", 200, b"2.11.8")
        with self.assertRaises(QBittorrentAuthError):
            QBittorrentClient(self.base_url).capabilities()


if __name__ == "__main__":
    unittest.main()
