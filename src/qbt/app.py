"""Interfaz de revisión local y consulta de solo lectura de qBittorrent."""

import sys
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .classification import classify
from .qbittorrent import QBittorrentClient, QBittorrentError

DEFAULT_URL = "http://10.0.4.56:8080"
DEFAULT_PATHS = (
    "/mnt/descargas/peliculas",
    "/mnt/descargas/series",
    "/mnt/descargas/libros",
)
PATH_KEYS = ("peliculas", "series", "libros")
PATH_LABELS = ("Películas", "Series", "Libros")
QUERY_TIMEOUT = 6.0  # inactividad por operación de socket, no plazo total


def validate_base_url(raw: str) -> str:
    """Normaliza y valida la URL base de la WebUI.

    Lanza ValueError con un mensaje apto para el usuario. urlsplit lanza
    ValueError con IPv6 mal formada o puertos fuera de rango, de modo que ese
    caso también se traduce aquí en vez de escapar como excepción en la GUI.
    """
    text = raw.strip()
    if not text:
        raise ValueError("URL no válida: escriba la dirección de la WebUI.")
    if any(char.isspace() for char in text):
        raise ValueError("URL no válida: no puede contener espacios.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError(
            "URL no válida: puerto o dirección IPv6 incorrectos."
        ) from None
    if parts.scheme not in {"http", "https"}:
        raise ValueError("URL no válida: use http:// o https:// con un host.")
    if not parts.hostname:
        raise ValueError("URL no válida: use http:// o https:// con un host.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL no válida: el puerto debe estar entre 1 y 65535.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL no válida: no se admiten credenciales en la URL.")
    if parts.query or parts.fragment:
        raise ValueError(
            "URL no válida: no se admite query ni fragmento; indique solo esquema, host y puerto."
        )
    return text.rstrip("/")


_STATE_LABELS = {
    "downloading": "Descargando",
    "stalledDL": "Descargando (sin pares)",
    "metaDL": "Obteniendo metadatos",
    "forcedDL": "Descargando (forzado)",
    "queuedDL": "En cola",
    "pausedDL": "En pausa",
    "stoppedDL": "Detenido",
    "uploading": "Compartiendo",
    "stalledUP": "Compartiendo (sin pares)",
    "forcedUP": "Compartiendo (forzado)",
    "queuedUP": "En cola",
    "pausedUP": "Completado (en pausa)",
    "stoppedUP": "Completado (detenido)",
    "checkingDL": "Comprobando",
    "checkingUP": "Comprobando",
    "queuedForChecking": "En cola de comprobación",
    "moving": "Moviendo",
    "error": "Error",
    "missingFiles": "Faltan archivos",
    "unknown": "Desconocido",
}


def _format_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            text = f"{value:.0f}" if unit == "B" else f"{value:.1f}"
            return f"{text} {unit}".replace(".", ",")
        value /= 1024
    return "—"


def _format_progress(progress: float | None) -> str:
    if progress is None:
        return "—"
    return f"{progress * 100:.1f} %".replace(".", ",")


class _QuerySignals(QObject):
    succeeded = Signal(int, str, object)
    failed = Signal(int, str)
    finished = Signal(int)


class _QueryTask(QRunnable):
    """Consulta en segundo plano; la cancelación descarta el resultado tardío.

    La marca es cooperativa: el hilo actual no se interrumpe ni se destruye.
    La señal ``finished`` se emite siempre, también tras cancelar, para que la
    ventana sepa cuándo el grupo de hilos vuelve a estar libre.
    """

    def __init__(self, request_id: int, operation, context, signals: _QuerySignals) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request_id = request_id
        self._operation = operation
        self._context = context
        self._signals = signals
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            result = self._operation()
        except QBittorrentError as exc:
            if not self._cancelled.is_set():
                self._signals.failed.emit(self.request_id, str(exc))
        except Exception:
            if not self._cancelled.is_set():
                self._signals.failed.emit(
                    self.request_id, "Error inesperado al consultar qBittorrent"
                )
        else:
            if not self._cancelled.is_set():
                self._signals.succeeded.emit(self.request_id, self._context, result)
        finally:
            self._signals.finished.emit(self.request_id)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Gestor qBittorrent — revisión local y consulta remota")
        self.resize(1000, 780)
        self.busy = False
        self._closing = False
        self._cancel_requested = False
        self._active_id: int | None = None
        self._request_id = 0
        self._tasks: dict[int, _QueryTask] = {}
        # Grupo propio (no el global) para que la ventana no arrastre trabajos
        # ajenos y para que el cierre no dependa de un waitForDone global.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        central = QWidget()
        root = QVBoxLayout(central)

        local_group = QGroupBox("Revisión local (sin conexión)")
        local_layout = QVBoxLayout(local_group)
        add_button = QPushButton("Añadir archivos .torrent…")
        add_button.clicked.connect(self.add_files)
        self.local_table = QTableWidget(0, 5)
        self.local_table.setHorizontalHeaderLabels(
            ["Archivo", "Tipo", "Título", "Temporada", "Confianza"]
        )
        self.local_table.horizontalHeader().setStretchLastSection(False)
        self.local_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 5):
            self.local_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.local_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        local_layout.addWidget(add_button)
        local_layout.addWidget(self.local_table)
        root.addWidget(local_group)

        remote_group = QGroupBox("qBittorrent — consulta de solo lectura")
        remote_layout = QVBoxLayout(remote_group)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("URL de la WebUI:"))
        self.url_edit = QLineEdit(DEFAULT_URL)
        self.url_edit.setPlaceholderText(DEFAULT_URL)
        self.url_edit.textChanged.connect(self._on_url_changed)
        url_row.addWidget(self.url_edit, 1)
        self.version_button = QPushButton("Consultar versión")
        self.version_button.clicked.connect(self.query_version)
        self.torrents_button = QPushButton("Consultar torrents")
        self.torrents_button.clicked.connect(self.query_torrents)
        self.cancel_button = QPushButton("Cancelar consulta")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_query)
        url_row.addWidget(self.version_button)
        url_row.addWidget(self.torrents_button)
        url_row.addWidget(self.cancel_button)
        remote_layout.addLayout(url_row)

        self.status_label = QLabel(
            "Sin conexión. Pulse «Consultar» para leer la instancia remota; no se guardan credenciales."
        )
        self.status_label.setWordWrap(True)
        # Las rutas remotas pueden contener "<" o "&": nunca interpretar HTML.
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        remote_layout.addWidget(self.status_label)

        self.torrents_table = QTableWidget(0, 4)
        self.torrents_table.setHorizontalHeaderLabels(["Nombre", "Estado", "Progreso", "Destino"])
        self.torrents_table.horizontalHeader().setStretchLastSection(False)
        self.torrents_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 4):
            self.torrents_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.torrents_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.torrents_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        remote_layout.addWidget(self.torrents_table, 2)

        paths_layout = QGridLayout()
        self.path_edits: dict[str, QLineEdit] = {}
        for row, (key, label, default) in enumerate(zip(PATH_KEYS, PATH_LABELS, DEFAULT_PATHS)):
            paths_layout.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(default)
            edit.setPlaceholderText(default)
            edit.textChanged.connect(self._sync_paths)
            self.path_edits[key] = edit
            paths_layout.addWidget(edit, row, 1)
        remote_layout.addLayout(paths_layout)

        directory_row = QHBoxLayout()
        directory_row.addWidget(QLabel("Directorio a consultar:"))
        self.directory_combo = QComboBox()
        self.directory_combo.addItems(DEFAULT_PATHS)
        directory_row.addWidget(self.directory_combo, 1)
        self.directory_button = QPushButton("Consultar directorio")
        self.directory_button.clicked.connect(self.query_directory)
        directory_row.addWidget(self.directory_button)
        remote_layout.addLayout(directory_row)

        self.directory_table = QTableWidget(0, 3)
        self.directory_table.setHorizontalHeaderLabels(["Nombre", "Tipo", "Tamaño"])
        self.directory_table.horizontalHeader().setStretchLastSection(False)
        self.directory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.directory_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.directory_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.directory_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.directory_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        remote_layout.addWidget(self.directory_table, 1)

        root.addWidget(remote_group, 1)
        self.setCentralWidget(central)
        self.table = self.local_table  # compatibilidad con la etapa anterior
        self.statusBar().showMessage(
            "Solo lectura: no se envía ningún torrent, no se vigila nada y no se guardan credenciales"
        )

    # --- revisión local -------------------------------------------------

    def add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Seleccionar torrents", "", "Archivos torrent (*.torrent)"
        )
        for filename in files:
            proposal = classify(filename)
            row = self.local_table.rowCount()
            self.local_table.insertRow(row)
            values = [
                proposal.source.name,
                proposal.kind,
                proposal.title,
                f"{proposal.season:02d}" if proposal.season is not None else "—",
                proposal.confidence,
            ]
            for column, value in enumerate(values):
                self.local_table.setItem(row, column, QTableWidgetItem(value))

    # --- consultas remotas ---------------------------------------------

    def _sync_paths(self) -> None:
        index = max(0, self.directory_combo.currentIndex())
        for position, key in enumerate(PATH_KEYS):
            text = self.path_edits[key].text().strip()
            self.directory_combo.setItemText(position, text or DEFAULT_PATHS[position])
        self.directory_combo.setCurrentIndex(index)

    def _show_error(self, message: str) -> None:
        self.status_label.setStyleSheet("color: #b00020;")
        self.status_label.setText(f"Error: {message}")

    def _show_status(self, message: str) -> None:
        self.status_label.setStyleSheet("color: #1a7f37;")
        self.status_label.setText(message)

    def _show_neutral(self, message: str) -> None:
        self.status_label.setStyleSheet("")
        self.status_label.setText(message)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for button in (self.version_button, self.torrents_button, self.directory_button):
            button.setEnabled(not busy)
        # Tras pedir la cancelación el botón se apaga, pero la ventana sigue
        # ocupada hasta que el hilo termina: nunca hay dos trabajos a la vez.
        self.cancel_button.setEnabled(busy and not self._cancel_requested)
        self.url_edit.setEnabled(not busy)

    def _on_url_changed(self, _text: str) -> None:
        """Cambiar de servidor invalida los datos del anterior."""
        if self._closing:
            return
        had_rows = bool(self.torrents_table.rowCount() or self.directory_table.rowCount())
        self.torrents_table.setRowCount(0)
        self.directory_table.setRowCount(0)
        if self.busy:
            self.cancel_query()
            return
        if had_rows:
            self._show_neutral(
                "URL cambiada: se han descartado los resultados del servidor anterior. "
                "Pulse «Consultar» para leer el nuevo servidor."
            )
        else:
            self._show_neutral(
                "Sin conexión. Pulse «Consultar» para leer la instancia remota; no se guardan credenciales."
            )

    def _start_query(self, operation, context: str, busy_text: str) -> None:
        if self.busy or self._closing:
            return
        try:
            base_url = validate_base_url(self.url_edit.text())
        except ValueError as exc:
            self._show_error(str(exc))
            return
        client = QBittorrentClient(base_url, timeout=QUERY_TIMEOUT)
        self._request_id += 1
        request_id = self._request_id
        signals = _QuerySignals()
        signals.succeeded.connect(self._on_success)
        signals.failed.connect(self._on_failure)
        signals.finished.connect(self._on_finished)
        task = _QueryTask(request_id, lambda: operation(client), context, signals)
        self._tasks[request_id] = task
        self._active_id = request_id
        self._cancel_requested = False
        self._set_busy(True)
        self._show_status(busy_text)
        self._pool.start(task)

    def query_version(self) -> None:
        self._start_query(
            lambda client: client.version(), "version", "Consultando versión de qBittorrent…"
        )

    def query_torrents(self) -> None:
        self._start_query(
            lambda client: client.torrents_info(include_files=False),
            "torrents",
            "Consultando torrents (solo lectura)…",
        )

    def query_directory(self) -> None:
        path = self.directory_combo.currentText().strip()
        if not path:
            self._show_error("Indique una ruta de directorio.")
            return
        if not path.startswith("/"):
            self._show_error("La ruta del directorio debe ser absoluta (empezar por /).")
            return
        self._start_query(
            lambda client: client.directory_content(path, with_metadata=True),
            f"directory:{path}",
            f"Consultando el nivel inmediato de {path}…",
        )

    def cancel_query(self) -> None:
        """Descarta el resultado sin liberar la ventana hasta que el hilo termine."""
        if not self.busy or self._cancel_requested:
            return
        task = self._tasks.get(self._active_id) if self._active_id is not None else None
        if task is not None:
            task.cancel()
        self._cancel_requested = True
        self.cancel_button.setEnabled(False)
        self._show_neutral(
            "Cancelación solicitada: se descarta la respuesta. No se puede lanzar otra "
            "consulta hasta que termine la actual."
        )

    @Slot(int, str, object)
    def _on_success(self, request_id: int, context: str, payload) -> None:
        if self._closing or self._cancel_requested or request_id != self._active_id:
            return
        if context == "version":
            self._show_status(f"Conectado. Versión de qBittorrent: {payload}")
        elif context == "torrents":
            self._fill_torrents(payload)
            self._show_status(f"{len(payload)} torrent(s) recibidos en modo solo lectura.")
        elif context.startswith("directory:"):
            path = context.split(":", 1)[1]
            self._fill_directory(payload)
            self._show_status(f"{len(payload)} entrada(s) en {path} (solo el nivel inmediato).")

    @Slot(int, str)
    def _on_failure(self, request_id: int, message: str) -> None:
        if self._closing or self._cancel_requested or request_id != self._active_id:
            return
        if self.torrents_table.rowCount() or self.directory_table.rowCount():
            message += " Se mantienen los últimos datos mostrados."
        self._show_error(message)

    @Slot(int)
    def _on_finished(self, request_id: int) -> None:
        self._tasks.pop(request_id, None)
        if request_id != self._active_id:
            return
        cancelled = self._cancel_requested
        self._active_id = None
        self._cancel_requested = False
        self._set_busy(False)
        if self._closing:
            # Cierre diferido: aceptar ahora que no quedan trabajos.
            self._show_neutral("Consulta finalizada; cerrando la ventana.")
            if not self._tasks:
                self.close()
            return
        if cancelled:
            self._show_neutral("Consulta cancelada; no se muestran datos de esa consulta.")

    def _fill_torrents(self, torrents: list[dict]) -> None:
        self.torrents_table.setRowCount(0)
        for torrent in torrents:
            row = self.torrents_table.rowCount()
            self.torrents_table.insertRow(row)
            state = torrent.get("state") or "—"
            values = [
                torrent.get("name", "—"),
                _STATE_LABELS.get(state, state),
                _format_progress(torrent.get("progress")),
                torrent.get("save_path") or "—",
            ]
            for column, value in enumerate(values):
                self.torrents_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _fill_directory(self, entries: list[dict]) -> None:
        self.directory_table.setRowCount(0)
        for entry in entries:
            row = self.directory_table.rowCount()
            self.directory_table.insertRow(row)
            is_dir = entry.get("type") == "dir"
            values = [
                entry.get("name", "—"),
                "Carpeta" if is_dir else "Archivo",
                "—" if is_dir else _format_size(entry.get("size")),
            ]
            for column, value in enumerate(values):
                self.directory_table.setItem(row, column, QTableWidgetItem(str(value)))

    def closeEvent(self, event) -> None:
        self._closing = True
        if self._tasks:
            # Sin waitForDone: se ignora el cierre, se cancela (marca
            # cooperativa, nunca terminate) y se cierra al llegar «finished».
            for task in self._tasks.values():
                task.cancel()
            self._cancel_requested = True
            self._set_busy(True)
            self.setEnabled(False)
            self._show_neutral(
                "Cerrando: se descarta la consulta en curso y la ventana se cerrará en cuanto "
                "termine. La interfaz no se bloquea."
            )
            event.ignore()
            return
        self._pool.clear()
        event.accept()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
