import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from qbt.qbittorrent import QBittorrentClient, QBittorrentError


class _Handler(BaseHTTPRequestHandler):
    """Servidor estricto: solo responde rutas exactas registradas por la prueba."""

    def do_GET(self):
        self.server.requests.append(
            {"method": self.command, "path": self.path, "authorization": self.headers.get("Authorization")}
        )
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, body, delay = route
        if delay:
            threading.Event().wait(delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.requests.append({"method": self.command, "path": self.path, "authorization": None})
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_args):
        pass


class QBittorrentClientTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.routes = {}
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def route(self, path, body, status=200, delay=0.0):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.server.routes[path] = (status, body, delay)

    def test_reads_server_version_with_get(self):
        self.route("/api/v2/app/version", "v5.3.0")
        client = QBittorrentClient(self.base_url)
        self.assertEqual(client.version(), "v5.3.0")
        self.assertEqual(self.server.requests[0]["method"], "GET")

    def test_reads_webapi_version(self):
        self.route("/api/v2/app/webapiVersion", "2.11.8")
        self.assertEqual(QBittorrentClient(self.base_url).webapi_version(), "2.11.8")

    def test_sends_optional_bearer_key(self):
        self.route("/api/v2/app/version", "v5.3.0")
        QBittorrentClient(self.base_url, api_key="qbt_test_key").version()
        self.assertEqual(self.server.requests[0]["authorization"], "Bearer qbt_test_key")

    def test_lists_torrents_without_files_by_default(self):
        self.route(
            "/api/v2/torrents/info?includeFiles=false",
            json.dumps([{"hash": "abc", "name": "sample", "state": "downloading", "progress": 0.25,
                         "save_path": "/mnt/descargas/peliculas"}]),
        )
        torrents = QBittorrentClient(self.base_url).torrents_info()
        self.assertEqual(torrents[0]["name"], "sample")

    def test_lists_torrents_with_file_metadata_enabled(self):
        self.route(
            "/api/v2/torrents/info?includeFiles=true",
            json.dumps([{"hash": "abc", "name": "sample", "state": "pausedUP"}]),
        )
        client = QBittorrentClient(self.base_url)
        self.assertEqual(client.torrents_info(include_files=True)[0]["hash"], "abc")

    def test_reads_remote_directory_with_exact_query(self):
        self.route(
            "/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas%2Fseries&withMetadata=true",
            json.dumps([{"name": "Serie", "type": "dir"},
                        {"name": "Capitulo.mkv", "type": "file", "size": 1234}]),
        )
        entries = QBittorrentClient(self.base_url).directory_content("/mnt/descargas/series")
        self.assertEqual([entry["name"] for entry in entries], ["Serie", "Capitulo.mkv"])
        query = parse_qs(urlsplit(self.server.requests[-1]["path"]).query)
        self.assertEqual(query["dirPath"], ["/mnt/descargas/series"])
        self.assertEqual(query["withMetadata"], ["true"])

    def test_403_is_reported_as_unauthorized_without_secret(self):
        self.route("/api/v2/app/version", b"", status=403)
        client = QBittorrentClient(self.base_url, api_key="qbt_secret_key")
        with self.assertRaises(QBittorrentError) as caught:
            client.version()
        message = str(caught.exception)
        self.assertIn("403", message)
        self.assertIn("no autorizado", message.lower())
        self.assertNotIn("qbt_secret_key", message)

    def test_http_error_response_is_closed_before_reporting(self):
        from urllib.error import HTTPError
        from urllib.request import urlopen
        from unittest import mock
        for status in (401, 403, 500):
            with self.subTest(status=status):
                self.route("/api/v2/app/version", b"denied", status=status)
                errors = []
                def capture(*args, **kwargs):
                    try:
                        return urlopen(*args, **kwargs)
                    except HTTPError as exc:
                        errors.append(exc)
                        raise
                try:
                    with mock.patch("qbt.qbittorrent.urlopen", side_effect=capture):
                        with self.assertRaises(QBittorrentError):
                            QBittorrentClient(self.base_url).version()
                    self.assertEqual(len(errors), 1)
                    self.assertTrue(errors[0].closed)
                finally:
                    for error in errors:
                        error.close()

    def test_empty_body_is_not_success(self):
        self.route("/api/v2/app/version", b"")
        with self.assertRaises(QBittorrentError) as caught:
            QBittorrentClient(self.base_url).version()
        self.assertIn("vacía", str(caught.exception))

    def test_malformed_json_is_not_success(self):
        self.route("/api/v2/torrents/info?includeFiles=false", b"<html>error</html>")
        with self.assertRaises(QBittorrentError) as caught:
            QBittorrentClient(self.base_url).torrents_info()
        self.assertIn("no válida", str(caught.exception))

    def test_torrents_response_must_be_a_list_of_objects(self):
        self.route("/api/v2/torrents/info?includeFiles=false", json.dumps({"name": "sample"}))
        with self.assertRaises(QBittorrentError):
            QBittorrentClient(self.base_url).torrents_info()

    def test_torrents_entries_without_name_are_rejected(self):
        self.route("/api/v2/torrents/info?includeFiles=false", json.dumps([{"state": "downloading"}]))
        with self.assertRaises(QBittorrentError):
            QBittorrentClient(self.base_url).torrents_info()

    def test_directory_entries_with_invalid_type_are_rejected(self):
        self.route(
            "/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas&withMetadata=true",
            json.dumps([{"name": "dudoso", "type": "symlink"}]),
        )
        with self.assertRaises(QBittorrentError):
            QBittorrentClient(self.base_url).directory_content("/mnt/descargas")

    def test_empty_torrent_list_is_a_valid_empty_result(self):
        self.route("/api/v2/torrents/info?includeFiles=false", b"[]")
        self.assertEqual(QBittorrentClient(self.base_url).torrents_info(), [])

    def test_response_size_limit_is_enforced(self):
        self.route("/api/v2/app/version", "v5.3.0" + "x" * 100)
        with self.assertRaises(QBittorrentError) as caught:
            QBittorrentClient(self.base_url, max_response_bytes=20).version()
        self.assertIn("límite", str(caught.exception).lower())

    def test_entry_limit_is_enforced(self):
        self.route(
            "/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas&withMetadata=true",
            json.dumps([{"name": f"f{i}", "type": "file", "size": i} for i in range(3)]),
        )
        with self.assertRaises(QBittorrentError) as caught:
            QBittorrentClient(self.base_url, max_entries=2).directory_content("/mnt/descargas")
        self.assertIn("límite", str(caught.exception).lower())

    def test_non_finite_or_out_of_range_progress_is_rejected(self):
        path = "/api/v2/torrents/info?includeFiles=false"
        for value in (float("nan"), float("inf"), float("-inf"), -0.1, 1.5):
            with self.subTest(progress=value):
                self.server.routes[path] = (
                    200,
                    json.dumps([{"name": "sample", "progress": value}]).encode("utf-8"),
                    0.0,
                )
                with self.assertRaises(QBittorrentError):
                    QBittorrentClient(self.base_url).torrents_info()

    def test_directory_entry_with_unhashable_type_is_rejected_without_type_error(self):
        path = "/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas&withMetadata=true"
        for bad_type in (["dir"], {"kind": "dir"}):
            with self.subTest(type=bad_type):
                self.server.routes[path] = (
                    200,
                    json.dumps([{"name": "dudoso", "type": bad_type}]).encode("utf-8"),
                    0.0,
                )
                with self.assertRaises(QBittorrentError):
                    QBittorrentClient(self.base_url).directory_content("/mnt/descargas")

    def test_progress_bounds_zero_and_one_are_accepted(self):
        self.route(
            "/api/v2/torrents/info?includeFiles=false",
            json.dumps([{"name": "a", "progress": 0}, {"name": "b", "progress": 1}]),
        )
        self.assertEqual(len(QBittorrentClient(self.base_url).torrents_info()), 2)

    def test_malformed_version_text_is_not_success(self):
        self.route("/api/v2/app/version", "<html>login</html>")
        with self.assertRaises(QBittorrentError):
            QBittorrentClient(self.base_url).version()


if __name__ == "__main__":
    unittest.main()
