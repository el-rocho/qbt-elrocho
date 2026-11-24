import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication

from qbt.app import DEFAULT_PATHS, DEFAULT_URL, MainWindow


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

    def test_version_query_shows_busy_state_and_result(self):
        self.route("/api/v2/app/version", "v5.3.0", delay=0.05)
        self.window.query_version()
        self.assertTrue(self.window.busy)
        self.assertFalse(self.window.version_button.isEnabled())
        self.assertTrue(self.window.cancel_button.isEnabled())
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("v5.3.0", self.window.status_label.text())
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
        status = self.window.status_label.text().lower()
        self.assertIn("403", status)
        self.assertIn("no autorizado", status)
        self.assertEqual(self.window.torrents_table.rowCount(), 2)

    def test_malformed_response_is_error_and_does_not_fill_rows(self):
        self.route(self.TORRENTS_PATH, json.dumps({"name": "no es lista"}))
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("Error", self.window.status_label.text())
        self.assertEqual(self.window.torrents_table.rowCount(), 0)

    def test_empty_body_is_error(self):
        self.route(self.TORRENTS_PATH, b"")
        self.window.query_torrents()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertIn("vacía", self.window.status_label.text())

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
        self.assertIn("cancel", self.window.status_label.text().lower())
        self.window.query_version()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.window._tasks), 1)
        self.assertLessEqual(self.window._pool.activeThreadCount(), 1)
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.assertIn("cancel", self.window.status_label.text().lower())
        self.assertTrue(self.window.torrents_button.isEnabled())
        self.assertTrue(self.window.url_edit.isEnabled())

    def test_cancel_discards_signals_already_queued_for_delivery(self):
        self.route(self.TORRENTS_PATH, self.TORRENTS_BODY, delay=0.3)
        self.window.query_torrents()
        request_id = self.window._active_id
        self.window.cancel_query()
        status = self.window.status_label.text()
        # Una señal emitida antes de cancelar puede entregarse después en Qt.
        self.window._on_success(request_id, "torrents", json.loads(self.TORRENTS_BODY))
        self.assertEqual(self.window.torrents_table.rowCount(), 0)
        self.window._on_failure(request_id, "Error tardío")
        self.assertEqual(self.window.status_label.text(), status)
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
        self.assertIn("límite", self.window.status_label.text().lower())
        self.assertEqual(self.window.directory_table.rowCount(), 0)

    def test_invalid_url_is_rejected_without_request(self):
        self.window.url_edit.setText("ftp://servidor")
        self.window.query_version()
        self.assertFalse(self.window.busy)
        self.assertIn("URL", self.window.status_label.text())
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
                self.assertIn("URL", self.window.status_label.text())
                self.assertEqual(self.requests(), [])

    def test_status_label_renders_remote_paths_as_plain_text(self):
        path = "/mnt/descargas/<b>series</b>"
        encoded = "/api/v2/app/getDirectoryContent?" + urlencode(
            {"dirPath": path, "withMetadata": "true"}
        )
        self.route(encoded, json.dumps([{"name": "Serie", "type": "dir"}]))
        self.window.path_edits["series"].setText(path)
        self.window.directory_combo.setCurrentIndex(1)
        self.window.query_directory()
        self.assertTrue(wait_until(lambda: not self.window.busy))
        self.assertEqual(self.window.status_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<b>series</b>", self.window.status_label.text())

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
        self.assertIn("descart", self.window.status_label.text().lower())

    def test_close_while_worker_active_defers_without_blocking(self):
        self.route("/api/v2/app/version", "v5.3.0", delay=0.5)
        self.window.query_version()
        self.assertTrue(self.window.busy)
        started = time.monotonic()
        self.window.close()
        self.assertLess(time.monotonic() - started, 0.25)  # no hay waitForDone bloqueante
        self.assertTrue(self.window.isVisible())          # cierre diferido
        self.assertTrue(self.window.busy)
        self.assertIn("cerr", self.window.status_label.text().lower())
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
        started = time.monotonic()
        self.window.close()
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertFalse(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
