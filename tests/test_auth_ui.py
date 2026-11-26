"""Sesión de conexión aislada, sin secretos persistidos."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from PySide6.QtTest import QTest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLineEdit

from qbt.app import MainWindow


class _AuthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, self.headers.get("Cookie")))
        if self.path != "/proxy/api/v2/auth/login":
            self.send_response(404)
            self.end_headers()
            return
        if parse_qs(body.decode()) != {"username": ["demo-user"], "password": ["demo-private"]}:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fails.")
            return
        self.send_response(self.server.login_status)
        self.send_header("Set-Cookie", "SID=demo-session; Path=/proxy/; HttpOnly")
        self.end_headers()
        if self.server.login_status != 204:
            self.wfile.write(b"Ok.")

    def do_GET(self):
        self.server.requests.append((self.command, self.path, self.headers.get("Cookie")))
        if self.server.delay:
            time.sleep(self.server.delay)
        if self.server.requires_auth and (
                self.headers.get("Cookie") != "SID=demo-session" or self.server.expired):
            self.send_response(403)
            self.end_headers()
            return
        routes = {
            "/proxy/api/v2/app/version": b"v5.3.0",
            "/proxy/api/v2/app/webapiVersion": self.server.api_version,
            "/proxy/api/v2/torrents/info?includeFiles=false": b'[{"name":"DEMO torrent"}]',
            "/proxy/api/v2/app/getDirectoryContent?dirPath=%2Flibrary&withMetadata=true": b'[]',
        }
        body = routes.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(body if body is not None else b"missing")

    def log_message(self, *_args):
        pass


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


class AuthUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/proxy"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.requests = []
        self.server.requires_auth = True
        self.server.expired = False
        self.server.delay = 0
        self.server.api_version = b"2.16.2"
        self.server.login_status = 200
        self.warning = mock.patch("qbt.app.QMessageBox.warning").start()
        self.information = mock.patch("qbt.app.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = MainWindow(
            settings=QSettings(str(self.root / "settings.ini"), QSettings.IniFormat),
            catalog_path=self.root / "catalog.sqlite3",
        )
        self.window._open_connection_config()
        self.dialog = self.window._setup_dialog

    def tearDown(self):
        self.window.close()
        QApplication.processEvents()
        self.window.deleteLater()
        QApplication.processEvents()
        self.temp.cleanup()

    def test_modern_204_login_can_be_applied_and_query_torrents(self):
        self.server.login_status = 204
        self.login()
        self.dialog.save_button.click()
        self.assertFalse(self.dialog.isVisible())
        self.assertTrue(self.window._client.authenticated)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 1)
        self.assertEqual(self.server.requests[-1][2], "SID=demo-session")
        self.warning.assert_not_called()

    def test_login_is_staged_until_save_then_used_for_queries(self):
        dialog = self.dialog
        dialog.url_edit.setText(self.url)
        dialog.auth_check.setChecked(True)
        dialog.username_edit.setText("demo-user")
        dialog.password_edit.setText("demo-private")
        dialog.access_button.click()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("2.16.2", dialog.status_label.text())
        self.assertEqual(dialog.password_edit.text(), "")
        self.assertFalse((self.root / "settings.json").exists())
        dialog.save_button.click()
        self.assertFalse(dialog.isVisible())
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 1)
        self.assertEqual(self.server.requests[-1], (
            "GET", "/proxy/api/v2/torrents/info?includeFiles=false", "SID=demo-session"))
        self.window.close()
        for path in self.root.iterdir():
            data = path.read_bytes()
            for secret in (b"demo-user", b"demo-private", b"demo-session"):
                self.assertNotIn(secret, data, path.name)

    def test_known_unsupported_functions_are_disabled_and_not_requested(self):
        from types import SimpleNamespace
        self.dialog.reject()
        self.window.url_edit.setText(self.url)
        self.window._capabilities = SimpleNamespace(torrents=True, directories=False)
        self.window._set_busy(False)
        self.assertTrue(self.window.torrents_button.isEnabled())
        self.assertFalse(self.window.directory_button.isEnabled())
        with mock.patch.object(self.window, "_run_batch") as batch:
            self.window.query_directories()
        batch.assert_not_called()
        self.assertIn("compatible", self.window.directory_status_label.text())
        self.assertTrue(self.window.local_group.isEnabled())

    def test_saving_another_server_discards_session_and_remote_results(self):
        from types import SimpleNamespace
        old_client = mock.Mock(base_url="http://localhost:9999")
        self.window._client = old_client
        self.window._capabilities = SimpleNamespace(torrents=True, directories=False)
        self.window._fill_torrents([{"name": "DEMO anterior"}])
        self.dialog.url_edit.setText(self.url)
        self.dialog.save_button.click()
        old_client.clear_session.assert_called_once()
        self.assertIsNone(self.window._client)
        self.assertIsNone(self.window._capabilities)
        self.assertEqual(self.window.torrents_table.rowCount(), 0)

    def login(self):
        self.dialog.url_edit.setText(self.url)
        self.dialog.auth_check.setChecked(True)
        self.dialog.username_edit.setText("demo-user")
        self.dialog.password_edit.setText("demo-private")
        self.dialog.access_button.click()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("2.16.2", self.dialog.status_label.text())

    def test_anonymous_server_still_works_and_can_check_capabilities(self):
        self.server.requires_auth = False
        self.dialog.url_edit.setText(self.url)
        self.dialog.access_button.click()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("sin credenciales", self.dialog.status_label.text())
        self.dialog.save_button.click()
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 1)
        self.assertFalse(any(method == "POST" for method, _, _ in self.server.requests))
        self.assertTrue(all(cookie is None for _, _, cookie in self.server.requests))

    def test_failed_login_keeps_dialog_and_does_not_activate_session(self):
        self.dialog.url_edit.setText(self.url)
        self.dialog.auth_check.setChecked(True)
        self.dialog.username_edit.setText("demo-user")
        self.dialog.password_edit.setText("wrong-password")
        self.dialog.access_button.click()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("rechaz", self.dialog.status_label.text().lower())
        self.assertEqual(self.dialog.password_edit.text(), "")
        self.assertIsNone(self.window._client)
        self.assertIsNone(self.window._setup_client)
        self.dialog.save_button.click()
        self.assertTrue(self.dialog.isVisible())
        self.assertFalse((self.root / "settings.json").exists())

    def test_cancelled_login_never_activates_late_session(self):
        self.server.delay = 0.2
        self.dialog.url_edit.setText(self.url)
        self.dialog.auth_check.setChecked(True)
        self.dialog.username_edit.setText("demo-user")
        self.dialog.password_edit.setText("demo-private")
        self.dialog.access_button.click()
        request_id = self.window._active_id
        self.assertTrue(self.window.busy)
        self.dialog.reject()
        from qbt.qbittorrent import ServerCapabilities
        self.window._on_success(request_id, "setup_access", ServerCapabilities("v5.3.0", "2.16.2", True, True))
        self.assertIsNone(self.window._setup_capabilities)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIsNone(self.window._client)
        self.assertIsNone(self.window._setup_client)
        self.assertFalse((self.root / "settings.json").exists())

    def test_editing_probe_credentials_invalidates_staged_session(self):
        self.login()
        candidate = self.window._setup_client
        self.dialog.username_edit.setText("different-user")
        self.assertFalse(candidate.authenticated)
        self.dialog.save_button.click()
        self.assertTrue(self.dialog.isVisible())
        self.assertFalse((self.root / "settings.json").exists())

    def test_session_expiry_requires_explicit_login_and_does_not_retry(self):
        self.login()
        self.dialog.save_button.click()
        client = self.window._client
        self.server.expired = True
        before = len(self.server.requests)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertFalse(client.authenticated)
        self.assertEqual(len(self.server.requests), before + 1)
        self.assertIn("Configuración", self.window.connection_label.text())
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.assertIsNone(self.window._capabilities)

    def test_expired_session_on_version_explains_how_to_login_again(self):
        self.login()
        self.dialog.save_button.click()
        self.server.expired = True
        self.window.query_version()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("Configuración", self.window.connection_label.text())

    def test_saving_offline_forgets_session_without_network_logout(self):
        self.login()
        self.dialog.save_button.click()
        active = self.window._client
        before = len(self.server.requests)
        self.window._open_connection_config()
        dialog = self.window._setup_dialog
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("offline"))
        dialog.save_button.click()
        self.assertFalse(active.authenticated)
        self.assertIsNone(self.window._client)
        self.assertFalse(self.window.torrents_button.isEnabled())
        self.assertEqual(len(self.server.requests), before)

    def test_authenticated_startup_batch_reuses_session(self):
        from dataclasses import replace
        from qbt.config import DirectoryConfig
        self.login()
        self.dialog.save_button.click()
        self.window._app_config = replace(
            self.window._app_config, directories=(DirectoryConfig("/library", "Libro"),)
        )
        self.window._rebuild_directory_tabs()
        before = len(self.server.requests)
        self.window.startup_query()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 1)
        self.assertIn("/library", self.window._directory_cache)
        self.assertEqual(len(self.server.requests), before + 2)
        self.assertTrue(all(cookie == "SID=demo-session" for _, _, cookie in self.server.requests[before:]))
        self.warning.assert_not_called()
        self.information.assert_called_once()

    def test_window_close_during_login_discards_session_and_waits(self):
        self.window.show()
        self.server.delay = 0.15
        self.dialog.url_edit.setText(self.url)
        self.dialog.auth_check.setChecked(True)
        self.dialog.username_edit.setText("demo-user")
        self.dialog.password_edit.setText("demo-private")
        self.dialog.access_button.click()
        self.window.close()
        self.assertTrue(self.window._closing)
        self.assertTrue(wait_until(lambda: not self.window.isVisible()))
        self.assertIsNone(self.window._client)
        self.assertIsNone(self.window._setup_client)
        self.assertEqual(self.dialog.password_edit.text(), "")
        self.assertFalse((self.root / "settings.json").exists())

    def test_failed_config_save_does_not_replace_existing_session(self):
        from qbt.config import ConfigError
        self.login()
        self.dialog.save_button.click()
        active = self.window._client
        self.window._open_connection_config()
        self.dialog = self.window._setup_dialog
        self.login()
        candidate = self.window._setup_client
        with mock.patch("qbt.app.save_config", side_effect=ConfigError("sin permiso")):
            self.dialog.save_button.click()
        self.assertTrue(self.dialog.isVisible())
        self.assertIs(self.window._client, active)
        self.assertTrue(active.authenticated)
        self.dialog.reject()
        self.assertFalse(candidate.authenticated)
        self.assertTrue(active.authenticated)

    def test_changing_server_in_header_forgets_cookie(self):
        self.login()
        self.dialog.save_button.click()
        active = self.window._client
        self.window.url_edit.setText(self.url + "/different")
        self.assertFalse(active.authenticated)
        self.assertIsNone(self.window._client)
        self.assertIsNone(self.window._capabilities)
        self.assertEqual(self.window.torrents_table.rowCount(), 0)

    def test_old_webapi_disables_only_directory_queries(self):
        self.server.api_version = b"2.11.7"
        self.dialog.url_edit.setText(self.url)
        self.server.requires_auth = False
        self.dialog.access_button.click()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("Directorios: no compatible", self.dialog.status_label.text())
        self.dialog.save_button.click()
        self.assertFalse(self.window.directory_button.isEnabled())
        self.assertTrue(self.window.torrents_button.isEnabled())
        self.assertTrue(self.window.local_group.isEnabled())

    def test_credentials_are_optional_masked_and_not_written(self):
        dialog = self.dialog
        self.assertFalse(dialog.auth_check.isChecked())
        self.assertEqual(dialog.password_edit.echoMode(), QLineEdit.Password)
        self.assertFalse(dialog.username_edit.isEnabled())
        dialog.auth_check.setChecked(True)
        self.assertTrue(dialog.username_edit.isEnabled())
        dialog.username_edit.setText("demo-user")
        dialog.password_edit.setText("demo-private")
        dialog.save_button.click()
        self.assertTrue(dialog.isVisible())
        self.assertIn("comprobar", dialog.status_label.text().lower())
        self.assertFalse((self.root / "settings.json").exists())
        dialog.reject()
        self.assertEqual(dialog.password_edit.text(), "")
        self.assertEqual(dialog.username_edit.text(), "")


if __name__ == "__main__":
    unittest.main()
