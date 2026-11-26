"""Primer inicio y configuración neutral con almacenamiento aislado."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from qbt.app import MainWindow
from qbt.config import AppConfig, DirectoryConfig, load, save


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "settings.json"
        self.windows = []

    def window(self):
        settings = QSettings(str(self.root / "settings.ini"), QSettings.IniFormat)
        window = MainWindow(settings=settings, catalog_path=self.root / "catalog.sqlite3")
        self.windows.append(window)
        return window

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        QApplication.processEvents()

    def test_save_connection_config_writes_json_exactly_once(self):
        window = self.window()
        window.startup_query()
        dialog = window._setup_dialog
        with mock.patch("qbt.app.save_config", wraps=save) as wrapped_save:
            dialog.save_button.click()
        self.assertEqual(wrapped_save.call_count, 1)

    def test_enter_in_url_does_not_start_network_or_save(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        window = self.window()
        window.startup_query()
        dialog = window._setup_dialog
        dialog.url_edit.setFocus()
        QApplication.processEvents()
        with mock.patch("qbt.app.QBittorrentClient") as client, mock.patch.object(window, "_dispatch_query"):
            QTest.keyClick(dialog.url_edit, Qt.Key_Return)
        client.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertTrue(dialog.isVisible())

    def test_fresh_start_has_no_server_no_directories_and_no_network(self):
        window = self.window()
        self.assertEqual(window.url_edit.text(), "")
        self.assertEqual(window._app_config.directories, ())
        self.assertEqual(window.directory_tabs.count(), 0)
        self.assertEqual(window.connection_config_button.toolTip(), str(self.path))
        with mock.patch("qbt.app.QBittorrentClient") as client:
            window.startup_query()
        client.assert_not_called()
        self.assertIsNotNone(window._setup_dialog)
        self.assertTrue(window._setup_dialog.isVisible())
        self.assertEqual(window._setup_dialog.url_edit.text(), "http://localhost:8080")
        self.assertFalse(self.path.exists())
        window._setup_dialog.reject()
        self.assertTrue(window.local_group.isEnabled())

    def test_offline_choice_is_saved_and_reopens_without_network_or_welcome(self):
        window = self.window()
        window.startup_query()
        dialog = window._setup_dialog
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("offline"))
        dialog.save_button.click()
        self.assertTrue(self.path.exists())
        self.assertTrue(load(self.path).setup_completed)
        self.assertEqual(load(self.path).connection_mode, "offline")
        self.assertEqual(load(self.path).base_url, "")
        self.assertFalse(dialog.isVisible())
        window.close()
        reopened = self.window()
        with mock.patch("qbt.app.QBittorrentClient") as client:
            reopened.startup_query()
            reopened.query_version()
            reopened.query_torrents()
            reopened.query_directories()
        client.assert_not_called()
        self.assertIsNone(reopened._setup_dialog)
        self.assertFalse(reopened.torrents_button.isEnabled())
        self.assertFalse(reopened.version_button.isEnabled())
        self.assertFalse(reopened.directory_button.isEnabled())
        self.assertFalse(reopened.url_edit.isEnabled())
        self.assertTrue(reopened.local_group.isEnabled())
        reopened.connection_config_button.click()
        self.assertTrue(reopened._setup_dialog.isVisible())

    def test_header_url_edit_does_not_complete_setup(self):
        window = self.window()
        window.startup_query()
        window._setup_dialog.reject()
        window.url_edit.setText("http://localhost:9090")
        self.assertTrue(self.path.exists())
        self.assertFalse(load(self.path).setup_completed)
        with mock.patch("qbt.app.QBittorrentClient") as client:
            window.startup_query()
        client.assert_not_called()
        self.assertTrue(window._setup_dialog.isVisible())

    def test_corrupt_config_is_visible_and_never_overwritten_by_ui(self):
        original = '{"version": 99, "private_data": "preserve"}'
        self.path.write_text(original, encoding="utf-8")
        window = self.window()
        self.assertIn("configuración", window.config_status_label.text().lower())
        self.assertEqual(window.url_edit.text(), "")
        self.assertFalse(window.directory_config_button.isEnabled())
        with mock.patch("qbt.app.QBittorrentClient") as client:
            window.startup_query()
        client.assert_not_called()
        self.assertIsNone(window._setup_dialog)
        window.url_edit.setText("http://localhost:9090")
        window.connection_config_button.click()
        dialog = window._setup_dialog
        dialog.save_button.click()
        self.assertTrue(dialog.isVisible())
        self.assertIn("conserva", dialog.status_label.text())
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_save_rejects_control_characters_before_trimming(self):
        window = self.window()
        window.startup_query()
        dialog = window._setup_dialog
        dialog.url_edit.setText("http://localhost:8080\t")
        dialog.save_button.click()
        self.assertFalse(self.path.exists())
        self.assertTrue(dialog.isVisible())
        self.assertIn("control", dialog.status_label.text())

    def test_save_local_and_disabled_autoquery_survive_edits_and_reopen(self):
        window = self.window()
        window.startup_query()
        dialog = window._setup_dialog
        dialog.startup_check.setChecked(False)
        dialog.save_button.click()
        saved = load(self.path)
        self.assertEqual(saved.base_url, "http://localhost:8080")
        self.assertEqual(saved.directories, ())
        self.assertTrue(saved.setup_completed)
        self.assertFalse(saved.query_on_startup)
        window.url_edit.setText("http://localhost:9090")
        self.assertFalse(load(self.path).query_on_startup)
        with mock.patch("qbt.app.DirectoryConfigDialog") as factory:
            factory.return_value.exec.return_value = 1
            factory.return_value.directories.return_value = [DirectoryConfig("/library", "Libro")]
            window._open_directory_config()
        saved = load(self.path)
        self.assertFalse(saved.query_on_startup)
        self.assertTrue(saved.setup_completed)
        self.assertEqual(saved.directories, (DirectoryConfig("/library", "Libro"),))
        window.close()
        reopened = self.window()
        with mock.patch("qbt.app.QBittorrentClient") as client:
            reopened.startup_query()
        client.assert_not_called()
        self.assertIsNone(reopened._setup_dialog)
        self.assertTrue(reopened.torrents_button.isEnabled())

    def test_failed_save_keeps_dialog_open_and_does_not_apply_draft(self):
        window = self.window()
        window.startup_query()
        dialog = window._setup_dialog
        from qbt.config import ConfigError
        with mock.patch("qbt.app.save_config", side_effect=ConfigError("sin permiso")):
            dialog.save_button.click()
        self.assertTrue(dialog.isVisible())
        self.assertIn("sin permiso", dialog.status_label.text())
        self.assertFalse(window._app_config.setup_completed)
        self.assertEqual(window.url_edit.text(), "")
        self.assertFalse(self.path.exists())

    def test_config_read_failures_do_not_connect_or_overwrite(self):
        for original in (b"broken json", b"\xff\xfe", b'{"version":1,"base_url":"ftp://host"}'):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                window = self.window()
                self.assertTrue(window.config_status_label.text())
                with mock.patch("qbt.app.QBittorrentClient") as client:
                    window.startup_query()
                client.assert_not_called()
                window.url_edit.setText("http://localhost:9999")
                with mock.patch("qbt.app.DirectoryConfigDialog") as factory:
                    window._open_directory_config()
                factory.assert_not_called()
                self.assertEqual(self.path.read_bytes(), original)
                window.close()

    def test_legacy_config_with_no_url_does_not_invent_a_server(self):
        self.path.write_text(json.dumps({"version": 1, "base_url": "", "directories": []}), encoding="utf-8")
        window = self.window()
        self.assertEqual(window.url_edit.text(), "")
        with mock.patch("qbt.app.QBittorrentClient") as client:
            window.startup_query()
        client.assert_not_called()
        self.assertTrue(window._setup_dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
