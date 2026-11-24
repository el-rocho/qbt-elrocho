import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from qbt.qbittorrent import QBittorrentClient


class _Handler(BaseHTTPRequestHandler):
    authorization = None
    paths = []

    def do_GET(self):
        type(self).authorization = self.headers.get("Authorization")
        type(self).paths.append(self.path)
        if self.path == "/api/v2/app/version":
            body = b"v5.3.0"
        elif self.path == "/api/v2/torrents/info?includeFiles=true":
            body = json.dumps([{"hash": "abc", "name": "sample"}]).encode()
        else:
            body = json.dumps([{"name": "Series", "type": "dir"}]).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class QBittorrentClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def test_reads_server_version(self):
        client = QBittorrentClient(self.base_url)
        self.assertEqual(client.version(), "v5.3.0")

    def test_sends_optional_bearer_key(self):
        client = QBittorrentClient(self.base_url, api_key="qbt_test_key")
        client.version()
        self.assertEqual(_Handler.authorization, "Bearer qbt_test_key")

    def test_lists_torrents_with_file_metadata_enabled(self):
        client = QBittorrentClient(self.base_url)
        self.assertEqual(client.torrents_info(include_files=True)[0]["hash"], "abc")
        self.assertEqual(_Handler.paths[-1], "/api/v2/torrents/info?includeFiles=true")

    def test_reads_remote_directory(self):
        client = QBittorrentClient(self.base_url)
        entries = client.directory_content("/mnt/descargas/series")
        self.assertEqual(entries[0]["name"], "Series")
        query = parse_qs(urlsplit(_Handler.paths[-1]).query)
        self.assertEqual(query["dirPath"], ["/mnt/descargas/series"])
        self.assertEqual(query["withMetadata"], ["true"])


if __name__ == "__main__":
    unittest.main()
