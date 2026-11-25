import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView

from qbt.app import (
    APPLICATION,
    COLUMN_WIDTHS_GROUP,
    DEFAULT_COLUMN_WIDTHS,
    DEFAULT_PATHS,
    DEFAULT_URL,
    IDLE_CONNECTION_TEXT,
    IDLE_DIRECTORY_TEXT,
    IDLE_TORRENTS_TEXT,
    ORGANIZATION,
    MainWindow,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Handler(BaseHTTPRequestHandler):
    """Servidor estricto: solo rutas exactas; nada de éxito por defecto."""

    def do_GET(self):
        with self.server.lock:
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
        with self.server.lock:
            self.server.requests.append({"method": self.command, "path": self.path, "authorization": None})
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_args):
        pass


def wait_until(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


def settings_user_dir() -> str:
    """Directorio que QSettings usa ahora para IniFormat/UserScope."""
    probe = QSettings(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORGANIZATION, APPLICATION
    )
    probe.setFallbacksEnabled(False)
    return os.path.dirname(str(probe.fileName()))


def isolate_settings(testcase, root: str) -> None:
    """Redirige IniFormat/UserScope a ``root`` y restaura al terminar la prueba.

    Ninguna prueba lee ni escribe las preferencias reales del usuario.
    """
    previous = settings_user_dir()
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, root)
    testcase.addCleanup(
        QSettings.setPath, QSettings.Format.IniFormat, QSettings.Scope.UserScope, previous
    )


def settings_file(root: str) -> str:
    return os.path.join(root, ORGANIZATION, f"{APPLICATION}.ini")


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def window_tables(window: MainWindow) -> dict[str, object]:
    return {
        "local": window.local_table,
        "torrents": window.torrents_table,
        "directory": window.directory_table,
    }


def apply_widths(window: MainWindow, widths: dict[str, list[int]]) -> None:
    tables = window_tables(window)
    for name, values in widths.items():
        for column, value in enumerate(values):
            tables[name].setColumnWidth(column, value)


def header_widths(window: MainWindow) -> dict[str, list[int]]:
    return {
        name: [
            table.horizontalHeader().sectionSize(column)
            for column in range(table.columnCount())
        ]
        for name, table in window_tables(window).items()
    }


class _MemorySettings:
    """Preferencias en memoria: separa el cierre de la latencia real de disco."""

    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass

    def status(self):
        return QSettings.Status.NoError


class _BrokenSettings:
    """Sustituto de QSettings que falla al escribir (disco lleno, permisos...)."""

    def value(self, _key, default=None):
        return default

    def setValue(self, _key, _value):
        raise RuntimeError("no se pudo escribir")

    def sync(self):
        pass

    def status(self):
        return QSettings.Status.NoError


_WRITE_WIDTHS_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from PySide6.QtWidgets import QApplication
    from qbt.app import MainWindow

    widths = json.loads(sys.argv[1])
    app = QApplication([])
    window = MainWindow()
    tables = {"local": window.local_table, "torrents": window.torrents_table,
              "directory": window.directory_table}
    for name, values in widths.items():
        for column, value in enumerate(values):
            tables[name].setColumnWidth(column, value)
    window.show()
    app.processEvents()
    window.close()
    app.processEvents()
    print(json.dumps({"closed": not window.isVisible()}))
    """
)

_READ_WIDTHS_SCRIPT = textwrap.dedent(
    """
    import json
    from PySide6.QtWidgets import QApplication
    from qbt.app import MainWindow

    app = QApplication([])
    window = MainWindow()
    tables = {"local": window.local_table, "torrents": window.torrents_table,
              "directory": window.directory_table}
    print(json.dumps({name: [t.horizontalHeader().sectionSize(i)
                             for i in range(t.columnCount())]
                      for name, t in tables.items()}))
    window.close()
    """
)


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.routes = {}
        cls.server.requests = []
        cls.server.lock = threading.Lock()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        with self.server.lock:
            self.server.routes = {}
            self.server.requests = []
        # Preferencias aisladas: nunca se toca ~/.config/el-rocho real.
        self.prefs = tempfile.TemporaryDirectory()
        self.addCleanup(self.prefs.cleanup)
        isolate_settings(self, self.prefs.name)
        self.window = MainWindow()
        self.window.url_edit.setText(self.base_url)
        self.window.show()

    def tearDown(self):
        # El cierre es diferido: no se destruye la ventana con un hilo vivo
        # (el destructor de QThreadPool bloquearía la interfaz).
        if self.window.busy:
            self.window.cancel_query()
            wait_until(lambda: not self.window.busy, timeout=3.0)
        self.window.close()
        if self.window.isVisible():
            wait_until(lambda: not self.window.isVisible(), timeout=3.0)
        self.window.deleteLater()
        QApplication.processEvents()

    def route(self, path, body, status=200, delay=0.0):
        if isinstance(body, str):
            body = body.encode("utf-8")
        with self.server.lock:
            self.server.routes[path] = (status, body, delay)

    def requests(self):
        with self.server.lock:
            return list(self.server.requests)

    TORRENTS_PATH = "/api/v2/torrents/info?includeFiles=false"
    TORRENTS_BODY = json.dumps([
        {"hash": "a", "name": "Pelicula A", "state": "downloading", "progress": 0.5,
         "save_path": "/mnt/descargas/peliculas"},
        {"hash": "b", "name": "Serie B", "state": "pausedUP", "progress": 1.0,
         "save_path": "/mnt/descargas/series"},
    ])

    # Anchos distintos de los de fábrica para cada columna de cada tabla.
    CUSTOM_WIDTHS = {
        "local": [171, 92, 233, 61, 118],
        "torrents": [251, 133, 87, 209],
        "directory": [311, 99, 123],
    }

    def test_remote_tabs_have_visual_separation_without_restyling_local_area(self):
        QApplication.processEvents()
        tabs = self.window.remote_tabs
        gap = tabs.geometry().top() - self.window.connection_label.geometry().bottom()
        self.assertGreaterEqual(gap, 16)
        self.assertGreater(tabs.tabBar().font().pointSizeF(), self.window.font().pointSizeF())
        self.assertIn("border: 1px solid palette(mid)", tabs.styleSheet())
        self.assertEqual(self.window.local_group.styleSheet(), "")
        self.assertEqual(self.window.local_table.font().pointSizeF(), self.window.font().pointSizeF())

    def test_all_tables_allow_manual_column_widths(self):
        for tab, table in ((0, self.window.local_table),
                           (0, self.window.torrents_table),
                           (1, self.window.directory_table)):
            with self.subTest(table=table):
                self.window.remote_tabs.setCurrentIndex(tab)
                QApplication.processEvents()
                header = table.horizontalHeader()
                for column in range(table.columnCount()):
                    self.assertEqual(header.sectionResizeMode(column), QHeaderView.Interactive)
                    header.resizeSection(column, 130 + column * 10)
                QApplication.processEvents()
                start = QPoint(header.sectionViewportPosition(0) + header.sectionSize(0) - 1,
                               header.height() // 2)
                QTest.mousePress(header.viewport(), Qt.LeftButton, pos=start)
                QTest.mouseMove(header.viewport(), start + QPoint(40, 0), delay=20)
                QTest.mouseRelease(header.viewport(), Qt.LeftButton, pos=start + QPoint(40, 0))
                QApplication.processEvents()
                self.assertEqual(header.sectionSize(0), 170)
                widths = [header.sectionSize(i) for i in range(table.columnCount())]
                self.window.resize(self.window.width() + 30, self.window.height())
                self.window._fill_torrents(json.loads(self.TORRENTS_BODY))
                self.window._fill_directory([{"name": "DEMO", "type": "dir"}])
                QApplication.processEvents()
                self.assertEqual([header.sectionSize(i) for i in range(table.columnCount())], widths)

    def test_startup_does_not_contact_server_and_keeps_local_table_separate(self):
        window = MainWindow()
        try:
            QTest.qWait(120)
            self.assertEqual(self.requests(), [])
            self.assertEqual(window.url_edit.text(), DEFAULT_URL)
            self.assertEqual(
                tuple(window.path_edits[key].text() for key in ("peliculas", "series", "libros")),
                DEFAULT_PATHS,
            )
            self.assertEqual(window.local_table.columnCount(), 5)
            self.assertEqual(window.local_table.rowCount(), 0)
            self.assertEqual(window.torrents_table.rowCount(), 0)
        finally:
            window.close()
            window.deleteLater()

    def test_remote_section_has_two_tabs_with_their_own_widgets(self):
        tabs = self.window.remote_tabs
        self.assertEqual(tabs.count(), 2)
        self.assertEqual(tabs.tabText(0), "Torrents")
        self.assertEqual(tabs.tabText(1), "Archivos descargados")
        torrents_page, directory_page = tabs.widget(0), tabs.widget(1)
        for widget in (
            self.window.torrents_table,
            self.window.torrents_button,
            self.window.torrents_status_label,
        ):
            self.assertTrue(torrents_page.isAncestorOf(widget))
        for widget in (
            self.window.directory_table,
            self.window.directory_button,
            self.window.directory_combo,
            self.window.directory_status_label,
            *self.window.path_edits.values(),
        ):
            self.assertTrue(directory_page.isAncestorOf(widget))
        # Ninguna tabla ni etiqueta se comparte entre pestañas.
        self.assertFalse(torrents_page.isAncestorOf(self.window.directory_table))
        self.assertFalse(torrents_page.isAncestorOf(self.window.directory_status_label))
        self.assertFalse(directory_page.isAncestorOf(self.window.torrents_table))
        self.assertFalse(directory_page.isAncestorOf(self.window.torrents_status_label))
        # La cabecera común (URL, versión, mensajes de conexión) queda fuera
        # de las pestañas y la revisión local se mantiene como sección superior.
        for widget in (
            self.window.url_edit,
            self.window.version_button,
            self.window.connection_label,
        ):
            self.assertFalse(torrents_page.isAncestorOf(widget))
            self.assertFalse(directory_page.isAncestorOf(widget))
        self.assertFalse(torrents_page.isAncestorOf(self.window.local_table))
        layout = self.window.centralWidget().layout()
        self.assertLess(
            layout.indexOf(self.window.local_group),
            layout.indexOf(self.window.remote_group),
        )
        for label in (
            self.window.connection_label,
            self.window.torrents_status_label,
            self.window.directory_status_label,
        ):
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)

    def test_torrents_messages_stay_in_torrents_tab_when_user_switches_tab(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.2)
        directory_before = self.window.directory_status_label.text()
        self.window.query_torrents()
        self.assertIn("Consultando torrents", self.window.torrents_status_label.text())
        # El usuario cambia de pestaña mientras la consulta sigue en curso.
        self.window.remote_tabs.setCurrentIndex(1)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("2 torrent", self.window.torrents_status_label.text())
        self.assertEqual(self.window.directory_status_label.text(), directory_before)
        self.assertEqual(self.window.remote_tabs.currentIndex(), 1)

    def test_torrents_error_is_reported_in_torrents_tab_after_switching_tab(self):
        self.route(self.TORRENTS_PATH, b"", status=403)
        directory_before = self.window.directory_status_label.text()
        self.window.query_torrents()
        self.window.remote_tabs.setCurrentIndex(1)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("403", self.window.torrents_status_label.text())
        self.assertIn("no autorizado", self.window.torrents_status_label.text().lower())
        self.assertEqual(self.window.directory_status_label.text(), directory_before)
        self.assertNotIn("403", self.window.directory_status_label.text())

    def test_directory_messages_stay_in_directory_tab_when_user_switches_tab(self):
        path = "/mnt/descargas/series"
        encoded = "/api/v2/app/getDirectoryContent?" + urlencode(
            {"dirPath": path, "withMetadata": "true"}
        )
        self.route(encoded, json.dumps([{"name": "Serie", "type": "dir"}]))
        self.window.path_edits["series"].setText(path)
        self.window.directory_combo.setCurrentIndex(1)
        torrents_before = self.window.torrents_status_label.text()
        self.window.remote_tabs.setCurrentIndex(0)
        self.window.query_directory()
        self.assertIn("Consultando el nivel inmediato", self.window.directory_status_label.text())
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("1 entrada", self.window.directory_status_label.text())
        self.assertEqual(self.window.torrents_status_label.text(), torrents_before)
        self.assertEqual(self.window.remote_tabs.currentIndex(), 0)

    def test_directory_path_validation_error_is_reported_in_directory_tab(self):
        self.window.path_edits["peliculas"].setText("relativa/sin/barra")
        self.window.directory_combo.setCurrentIndex(0)
        self.window.remote_tabs.setCurrentIndex(0)
        self.window.query_directory()
        self.assertIn("absoluta", self.window.directory_status_label.text())
        self.assertEqual(self.window.connection_label.text(), IDLE_CONNECTION_TEXT)
        self.assertEqual(self.window.torrents_status_label.text(), IDLE_TORRENTS_TEXT)
        self.assertEqual(self.requests(), [])

    def test_cancellation_messages_stay_in_origin_tab_after_switching_tab(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.4)
        directory_before = self.window.directory_status_label.text()
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: len(self.requests()) >= 1))
        self.window.remote_tabs.setCurrentIndex(1)
        self.window.cancel_query()
        self.assertIn("cancelación", self.window.torrents_status_label.text().lower())
        self.assertEqual(self.window.directory_status_label.text(), directory_before)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("cancelada", self.window.torrents_status_label.text().lower())
        self.assertEqual(self.window.directory_status_label.text(), directory_before)

    def test_directory_cancellation_is_reported_in_directory_tab(self):
        path = "/mnt/descargas/peliculas"
        encoded = "/api/v2/app/getDirectoryContent?" + urlencode(
            {"dirPath": path, "withMetadata": "true"}
        )
        self.route(encoded, json.dumps([{"name": "x", "type": "dir"}]), delay=0.4)
        torrents_before = self.window.torrents_status_label.text()
        self.window.remote_tabs.setCurrentIndex(1)
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: len(self.requests()) >= 1))
        self.window.remote_tabs.setCurrentIndex(0)
        self.window.cancel_query()
        self.assertIn("cancel", self.window.directory_status_label.text().lower())
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("cancelada", self.window.directory_status_label.text().lower())
        self.assertEqual(self.window.torrents_status_label.text(), torrents_before)
        self.assertEqual(self.window.directory_table.rowCount(), 0)

    def test_directory_failure_is_reported_in_directory_tab_after_switching_tab(self):
        path = "/mnt/descargas/libros"
        encoded = "/api/v2/app/getDirectoryContent?" + urlencode(
            {"dirPath": path, "withMetadata": "true"}
        )
        self.route(encoded, b"", status=500)
        torrents_before = self.window.torrents_status_label.text()
        self.window.path_edits["libros"].setText(path)
        self.window.directory_combo.setCurrentIndex(2)
        self.window.remote_tabs.setCurrentIndex(1)
        self.window.query_directory()
        self.window.remote_tabs.setCurrentIndex(0)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("500", self.window.directory_status_label.text())
        self.assertEqual(self.window.torrents_status_label.text(), torrents_before)
        self.assertEqual(self.window.torrents_table.rowCount(), 0)

    def test_failure_only_mentions_kept_rows_of_its_own_tab(self):
        directory_path = "/mnt/descargas/peliculas"
        encoded = "/api/v2/app/getDirectoryContent?" + urlencode(
            {"dirPath": directory_path, "withMetadata": "true"}
        )
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.route(encoded, json.dumps([{"name": "Carpeta", "type": "dir"}]))
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 2)
        self.assertEqual(self.window.directory_table.rowCount(), 1)

        # Falla torrents: conserva sus filas y no toca la otra pestaña.
        self.route(self.TORRENTS_PATH, b"", status=403)
        directory_label_before = self.window.directory_status_label.text()
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("Se mantienen", self.window.torrents_status_label.text())
        self.assertEqual(self.window.directory_status_label.text(), directory_label_before)
        self.assertEqual(self.window.directory_table.rowCount(), 1)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)

        # Falla el directorio: no hay filas propias previas que conservar,
        # así que no debe afirmarlo ni alterar la tabla de torrents.
        torrents_label_before = self.window.torrents_status_label.text()
        self.window.directory_table.setRowCount(0)
        self.route(encoded, b"", status=500)
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertNotIn("Se mantienen", self.window.directory_status_label.text())
        self.assertIn("500", self.window.directory_status_label.text())
        self.assertEqual(self.window.torrents_status_label.text(), torrents_label_before)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)
        self.assertEqual(self.window.directory_table.rowCount(), 0)

    def test_version_query_uses_common_header_and_keeps_both_tables(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        torrents_summary = self.window.torrents_status_label.text()
        directory_summary = self.window.directory_status_label.text()
        self.route("/api/v2/app/version", "v5.3.0")
        self.window.remote_tabs.setCurrentIndex(1)
        self.window.query_version()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("v5.3.0", self.window.connection_label.text())
        self.assertEqual(self.window.torrents_status_label.text(), torrents_summary)
        self.assertEqual(self.window.directory_status_label.text(), directory_summary)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)
        self.assertEqual(self.window.directory_table.rowCount(), 0)

    def test_url_change_resets_tab_messages_and_tables_of_previous_server(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("2 torrent", self.window.torrents_status_label.text())
        self.window.url_edit.setText("http://127.0.0.1:1")
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.assertEqual(self.window.directory_table.rowCount(), 0)
        self.assertEqual(self.window.torrents_status_label.text(), IDLE_TORRENTS_TEXT)
        self.assertEqual(self.window.directory_status_label.text(), IDLE_DIRECTORY_TEXT)
        self.assertIn("descart", self.window.connection_label.text().lower())

    def test_version_query_shows_busy_state_and_result(self):
        self.route("/api/v2/app/version", "v5.3.0", delay=0.05)
        self.window.query_version()
        self.assertTrue(self.window.busy)
        self.assertFalse(self.window.version_button.isEnabled())
        self.assertTrue(self.window.cancel_button.isEnabled())
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("v5.3.0", self.window.connection_label.text())
        self.assertEqual([r["path"] for r in self.requests()], ["/api/v2/app/version"])
        self.assertIsNone(self.requests()[0]["authorization"])

    def test_torrents_query_fills_read_only_table(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        table = self.window.torrents_table
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(0, 0).text(), "Pelicula A")
        self.assertEqual(table.item(0, 1).text(), "Descargando")
        self.assertIn("50", table.item(0, 2).text())
        self.assertEqual(table.item(0, 3).text(), "/mnt/descargas/peliculas")
        self.assertEqual(table.editTriggers(), QAbstractItemView.NoEditTriggers)
        self.assertEqual(self.requests()[0]["path"], self.TORRENTS_PATH)

    def test_forbidden_response_is_clear_and_keeps_previous_rows(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.route(self.TORRENTS_PATH, b"", status=403)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        status = self.window.torrents_status_label.text().lower()
        self.assertIn("403", status)
        self.assertIn("no autorizado", status)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)

    def test_malformed_response_is_error_and_does_not_fill_rows(self):
        self.route(self.TORRENTS_PATH, json.dumps({"name": "no es lista"}))
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("Error", self.window.torrents_status_label.text())
        self.assertEqual(self.window.torrents_table.rowCount(), 0)

    def test_empty_body_is_error(self):
        self.route(self.TORRENTS_PATH, b"")
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("vacía", self.window.torrents_status_label.text())

    def test_duplicate_query_is_ignored_and_ui_not_blocked(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.5)
        started = time.monotonic()
        self.window.query_torrents()
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(self.window.busy)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)

    def test_cancel_keeps_busy_and_blocks_new_queries_until_task_ends(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.5)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: len(self.requests()) >= 1))
        self.window.cancel_query()
        # La cancelación no libera el estado ocupado: se descarta el resultado,
        # pero no se permite acumular otro trabajo en el grupo de hilos.
        self.assertTrue(self.window.busy)
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.assertFalse(self.window.torrents_button.isEnabled())
        self.assertFalse(self.window.version_button.isEnabled())
        self.assertFalse(self.window.url_edit.isEnabled())
        self.assertIn("cancel", self.window.torrents_status_label.text().lower())
        self.window.query_version()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.window._tasks), 1)
        self.assertLessEqual(self.window._pool.activeThreadCount(), 1)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.assertIn("cancel", self.window.torrents_status_label.text().lower())
        self.assertTrue(self.window.torrents_button.isEnabled())
        self.assertTrue(self.window.url_edit.isEnabled())

    def test_cancel_discards_signals_already_queued_for_delivery(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.3)
        self.window.query_torrents()
        request_id = self.window._active_id
        self.window.cancel_query()
        status = self.window.torrents_status_label.text()
        directory_status = self.window.directory_status_label.text()
        # Una señal emitida antes de cancelar puede entregarse después en Qt.
        self.window._on_success(request_id, "torrents", json.loads(self.TORRENTS_BODY))
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.window._on_failure(request_id, "torrents", "Error tardío")
        self.assertEqual(self.window.torrents_status_label.text(), status)
        self.assertEqual(self.window.directory_status_label.text(), directory_status)
        self.assertTrue(wait_until(lambda: not self.window.busy))

    def test_cancelled_query_can_be_repeated_after_it_ends(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.3)
        self.window.query_torrents()
        self.window.cancel_query()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(len(self.requests()), 2)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)

    def test_directory_query_uses_selected_configurable_path(self):
        path = "/mnt/descargas/series"
        entries = json.dumps([{"name": "Serie", "type": "dir"},
                              {"name": "Capitulo.mkv", "type": "file", "size": 1234}])
        encoded = "/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas%2Fseries&withMetadata=true"
        self.route(encoded, entries)
        self.window.path_edits["series"].setText(path)
        self.window.directory_combo.setCurrentIndex(1)
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.requests()[0]["path"], encoded)
        table = self.window.directory_table
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(0, 1).text(), "Carpeta")
        self.assertEqual(table.item(1, 1).text(), "Archivo")
        self.assertIn("1,2", table.item(1, 2).text())
        query = parse_qs(urlsplit(self.requests()[0]["path"]).query)
        self.assertEqual(query["dirPath"], [path])
        self.assertEqual(query["withMetadata"], ["true"])

    def test_directory_entry_limit_is_reported(self):
        path = "/mnt/descargas/peliculas"
        entries = json.dumps([{"name": f"f{i}", "type": "file", "size": 1} for i in range(2001)])
        self.route("/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas%2Fpeliculas&withMetadata=true",
                   entries)
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("límite", self.window.directory_status_label.text().lower())
        self.assertEqual(self.window.directory_table.rowCount(), 0)

    def test_version_error_does_not_claim_directory_rows_as_version_data(self):
        self.window._fill_directory([{"name": "DEMO", "type": "dir"}])
        self.route("/api/v2/app/version", b"", status=403)
        self.window.query_version()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("403", self.window.connection_label.text())
        self.assertNotIn("Se mantienen", self.window.connection_label.text())
        self.assertEqual(self.window.directory_table.rowCount(), 1)

    def test_invalid_url_is_rejected_without_request(self):
        self.window.url_edit.setText("ftp://servidor")
        self.window.query_version()
        self.assertFalse(self.window.busy)
        self.assertIn("URL", self.window.connection_label.text())
        self.assertEqual(self.requests(), [])

    def test_malformed_or_unsafe_urls_are_rejected_without_crash(self):
        cases = (
            "http://[::1",                       # IPv6 malformada: urlsplit lanza ValueError
            "http://[zz::1]/",                   # IPv6 no hexadecimal
            "http://127.0.0.1:99999",            # puerto fuera de rango
            "http://127.0.0.1:abc",              # puerto no numérico
            "http://usuario:clave@127.0.0.1:8080",  # credenciales en la URL
            "http://127.0.0.1:8080/?a=1",        # query
            "http://127.0.0.1:8080/#frag",       # fragmento
            "http://",                           # sin host
            "   ",                               # vacía
        )
        for url in cases:
            with self.subTest(url=url):
                self.window.url_edit.setText(url)
                self.window.query_version()
                self.assertFalse(self.window.busy)
                self.assertIn("URL", self.window.connection_label.text())
                self.assertEqual(self.requests(), [])

    def test_tab_labels_render_remote_paths_as_plain_text(self):
        path = "/mnt/descargas/<b>series</b>"
        encoded = "/api/v2/app/getDirectoryContent?" + urlencode(
            {"dirPath": path, "withMetadata": "true"}
        )
        self.route(encoded, json.dumps([{"name": "Serie", "type": "dir"}]))
        self.window.path_edits["series"].setText(path)
        self.window.directory_combo.setCurrentIndex(1)
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(
            self.window.directory_status_label.textFormat(), Qt.TextFormat.PlainText
        )
        self.assertIn("<b>series</b>", self.window.directory_status_label.text())

    def test_url_change_discards_previous_results(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.route(
            "/api/v2/app/getDirectoryContent?dirPath=%2Fmnt%2Fdescargas%2Fpeliculas&withMetadata=true",
            json.dumps([{"name": "Carpeta", "type": "dir"}]),
        )
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 2)
        self.assertEqual(self.window.directory_table.rowCount(), 1)
        self.window.url_edit.setText("http://127.0.0.1:1")
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.assertEqual(self.window.directory_table.rowCount(), 0)
        self.assertIn("descart", self.window.connection_label.text().lower())

    def test_close_while_worker_active_defers_without_blocking(self):
        self.route("/api/v2/app/version", "v5.3.0", delay=0.5)
        self.window.query_version()
        self.assertTrue(self.window.busy)
        started = time.monotonic()
        self.window.close()
        self.assertLess(time.monotonic() - started, 0.25)  # no hay waitForDone bloqueante
        self.assertTrue(self.window.isVisible())          # cierre diferido
        self.assertTrue(self.window.busy)
        self.assertIn("cerr", self.window.connection_label.text().lower())
        self.assertTrue(wait_until(lambda: not self.window.isVisible(), timeout=3.0))
        self.assertFalse(self.window.busy)

    def test_url_change_during_query_discards_active_request(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.4)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: len(self.requests()) >= 1))
        self.assertFalse(self.window.url_edit.isEnabled())
        # Cambio programático (la UI lo impide): debe descartar la consulta.
        self.window.url_edit.setText("http://127.0.0.1:1")
        self.assertTrue(self.window.busy)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.assertEqual(len(self.requests()), 1)

    def test_close_after_cancel_finishes_when_task_ends(self):
        self.route("/api/v2/app/version", "v5.3.0", delay=0.4)
        self.window.query_version()
        self.assertTrue(wait_until(lambda: len(self.requests()) >= 1))
        self.window.cancel_query()
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(wait_until(lambda: not self.window.isVisible(), timeout=3.0))
        self.assertFalse(self.window.busy)

    def test_close_when_idle_is_immediate(self):
        # La escritura real de QSettings en disco puede tardar; aquí se mide
        # que el cierre no espera a ningún trabajador (sin waitForDone).
        window = MainWindow(settings=_MemorySettings())
        window.show()
        try:
            started = time.monotonic()
            window.close()
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertFalse(window.isVisible())
        finally:
            window.deleteLater()
            QApplication.processEvents()

    # --- persistencia de anchos de columna ------------------------------

    def stored_widths(self):
        settings = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORGANIZATION, APPLICATION
        )
        settings.setFallbacksEnabled(False)
        return {
            name: [
                int(value) for value in settings.value(f"{COLUMN_WIDTHS_GROUP}/{name}")
            ]
            for name in self.CUSTOM_WIDTHS
        }

    def run_child(self, script, *args):
        """Ejecuta un proceso hijo real con las mismas preferencias aisladas."""
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = self.prefs.name
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, "-c", script, *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip().splitlines()[-1]

    def test_widths_are_saved_on_close_and_restored_by_new_window(self):
        apply_widths(self.window, self.CUSTOM_WIDTHS)
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.stored_widths(), self.CUSTOM_WIDTHS)

        restored = MainWindow()
        try:
            self.assertEqual(header_widths(restored), self.CUSTOM_WIDTHS)
            self.assertEqual(restored.local_table.rowCount(), 0)
        finally:
            restored.close()
            restored.deleteLater()
            QApplication.processEvents()

    def test_real_roundtrip_across_processes_restores_all_columns_of_three_tables(self):
        written = self.run_child_with_widths()
        self.assertTrue(written["closed"])

        restored = json.loads(self.run_child(_READ_WIDTHS_SCRIPT))
        self.assertEqual(
            restored,
            {name: list(values) for name, values in self.CUSTOM_WIDTHS.items()},
        )

        # El fichero real en disco solo contiene los anchos de las tres tablas.
        path = settings_file(self.prefs.name)
        self.assertTrue(os.path.isfile(path))
        settings = QSettings(path, QSettings.Format.IniFormat)
        self.assertEqual(
            sorted(settings.allKeys()),
            sorted(f"{COLUMN_WIDTHS_GROUP}/{name}" for name in self.CUSTOM_WIDTHS),
        )
        text = read_text(path)
        for forbidden in (DEFAULT_URL, *DEFAULT_PATHS, "Pelicula A", "Serie B"):
            self.assertNotIn(forbidden, text)

    def run_child_with_widths(self):
        return json.loads(
            self.run_child(_WRITE_WIDTHS_SCRIPT, json.dumps(self.CUSTOM_WIDTHS))
        )

    def test_corrupt_stored_widths_keep_defaults(self):
        cases = {
            "valor no numérico": "torrents=360, ochenta, 100, 300",
            "lista incompleta": "torrents=360, 180",
            "ancho negativo": "torrents=-360, 180, 100, 300",
            "ancho desmesurado": "torrents=999999, 180, 100, 300",
            "valor escalar": "torrents=360",
            "valor vacío": "torrents=",
        }
        for case, line in cases.items():
            with self.subTest(case=case):
                # Fichero propio por caso: evita la caché interna de QSettings
                # al reescribir el mismo .ini varias veces.
                root = tempfile.mkdtemp(dir=self.prefs.name)
                QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, root)
                path = settings_file(root)
                os.makedirs(os.path.dirname(path))
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(
                        "[column_widths]\n"
                        "local=301, 121, 281, 101, 102\n"
                        f"{line}\n"
                        "directory=540\n"
                    )
                window = MainWindow()
                try:
                    widths = header_widths(window)
                    # La tabla válida se restaura aunque las otras estén corruptas.
                    self.assertEqual(widths["local"], [301, 121, 281, 101, 102])
                    self.assertEqual(
                        widths["torrents"], list(DEFAULT_COLUMN_WIDTHS["torrents"])
                    )
                    self.assertEqual(
                        widths["directory"], list(DEFAULT_COLUMN_WIDTHS["directory"])
                    )
                finally:
                    window.close()
                    window.deleteLater()
                    QApplication.processEvents()

    def test_close_with_active_query_saves_only_when_window_really_closes(self):
        self.route("/api/v2/app/version", "v5.3.0", delay=0.5)
        apply_widths(self.window, self.CUSTOM_WIDTHS)
        self.window.query_version()
        self.assertTrue(self.window.busy)
        self.window.close()
        # Cierre diferido: la ventana sigue abierta y aún no se ha guardado nada.
        self.assertTrue(self.window.isVisible())
        self.assertFalse(os.path.exists(settings_file(self.prefs.name)))
        self.assertTrue(wait_until(lambda: not self.window.isVisible(), timeout=4.0))
        self.assertFalse(self.window.busy)
        self.assertEqual(self.stored_widths(), self.CUSTOM_WIDTHS)

        restored = MainWindow()
        try:
            self.assertEqual(header_widths(restored), self.CUSTOM_WIDTHS)
        finally:
            restored.close()
            restored.deleteLater()
            QApplication.processEvents()

    def test_save_error_on_close_is_visible_and_still_exits(self):
        # Un directorio ocupando la ruta del .ini provoca AccessError al
        # escribir, también si las pruebas se ejecutan como root.
        root = tempfile.mkdtemp(dir=self.prefs.name)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, root)
        os.makedirs(settings_file(root))

        window = MainWindow()
        window.show()
        apply_widths(window, self.CUSTOM_WIDTHS)
        try:
            with mock.patch("qbt.app.QMessageBox.warning") as warning:
                window.close()
            self.assertFalse(window.isVisible())
            self.assertEqual(warning.call_count, 1)
            title, message = warning.call_args.args[1:3]
            self.assertIn("ancho", title.lower())
            self.assertIn("No se pudieron guardar", message)
        finally:
            window.deleteLater()
            QApplication.processEvents()
        self.assertFalse(os.path.isfile(settings_file(root)))

    def test_deferred_close_with_save_failure_still_closes(self):
        # Cierre diferido (consulta en curso) + fallo al guardar: el aviso no
        # puede dejar la ventana abierta ni _closing colgado para siempre.
        root = tempfile.mkdtemp(dir=self.prefs.name)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, root)
        os.makedirs(settings_file(root))
        window = MainWindow()
        window.show()
        window.url_edit.setText(self.base_url)
        apply_widths(window, self.CUSTOM_WIDTHS)
        self.route("/api/v2/app/version", "v5.3.0", delay=0.4)
        window.query_version()
        self.assertTrue(wait_until(lambda: len(self.requests()) >= 1))
        try:
            with mock.patch("qbt.app.QMessageBox.warning") as warning:
                window.close()
                self.assertTrue(window.isVisible())
                self.assertTrue(
                    wait_until(lambda: not window.isVisible(), timeout=4.0)
                )
            self.assertFalse(window.busy)
            self.assertEqual(warning.call_count, 1)
        finally:
            window.deleteLater()
            QApplication.processEvents()

    def test_unexpected_save_failure_still_closes_with_warning(self):
        window = MainWindow(settings=_BrokenSettings())
        window.show()
        apply_widths(window, self.CUSTOM_WIDTHS)
        try:
            with mock.patch("qbt.app.QMessageBox.warning") as warning:
                window.close()
            self.assertFalse(window.isVisible())
            self.assertEqual(warning.call_count, 1)
        finally:
            window.deleteLater()
            QApplication.processEvents()

    def test_only_column_widths_are_persisted(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY)
        self.window.path_edits["series"].setText("/mnt/privado/series")
        self.window.url_edit.setText(self.base_url)
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        apply_widths(self.window, self.CUSTOM_WIDTHS)
        self.window.close()

        path = settings_file(self.prefs.name)
        text = read_text(path)
        self.assertEqual(self.stored_widths(), self.CUSTOM_WIDTHS)
        for forbidden in (DEFAULT_URL, "/mnt/privado/series", "Pelicula A", "Serie B"):
            self.assertNotIn(forbidden, text)
        settings = QSettings(path, QSettings.Format.IniFormat)
        self.assertEqual(
            sorted(settings.allKeys()),
            sorted(f"{COLUMN_WIDTHS_GROUP}/{name}" for name in self.CUSTOM_WIDTHS),
        )


if __name__ == "__main__":
    unittest.main()
