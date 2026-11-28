"""Interfaz de revisión local y consulta de solo lectura de qBittorrent."""

import sys
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import (
    AppConfig,
    ConfigError,
    DirectoryConfig,
    load as load_config,
    save as save_config,
)
from .connection_dialog import ConnectionDialog, LOCAL_URL_SUGGESTION
from .directory_dialog import DirectoryConfigDialog
from .local_review import LocalReviewPanel
from .qbittorrent import QBittorrentClient, QBittorrentError
from .style import TABLE_SELECTION_QSS

# Icono de la aplicación (logo .torrent incluido en el paquete).
ASSETS_DIR = Path(__file__).parent / "assets"
APP_ICON_PATH = ASSETS_DIR / "qbt-elrocho.png"


def _load_app_icon() -> QIcon:
    """Devuelve el icono empaquetado; QIcon vacío si el recurso no está."""
    if APP_ICON_PATH.is_file():
        return QIcon(str(APP_ICON_PATH))
    return QIcon()

QUERY_TIMEOUT = 6.0  # inactividad por operación de socket, no plazo total
# Persistencia de anchos de columna: solo geometría de la interfaz local.
ORGANIZATION = "el-rocho"
APPLICATION = "qbt-elrocho"
COLUMN_WIDTHS_GROUP = "column_widths"
DEFAULT_COLUMN_WIDTHS = {
    "local": (300, 110, 260, 90, 100, 280, 70, 300),
    "torrents": (360, 180, 100, 300),
    "directory": (540, 120, 140),
}
MIN_COLUMN_WIDTH = 1
MAX_COLUMN_WIDTH = 4000
IDLE_CONNECTION_TEXT = (
    "Sin conexión. Pulse «Consultar versión» o consulte desde una pestaña; "
    "no se guardan credenciales."
)
IDLE_TORRENTS_TEXT = (
    "Sin consultar. Pulse «Consultar torrents» para leer la lista remota (solo lectura)."
)
IDLE_DIRECTORY_TEXT = (
    "Sin consultar. Pulse «Consultar directorios» para leer el nivel inmediato "
    "de cada directorio configurado."
)
# Contexto de las validaciones locales de la pestaña «Archivos descargados»:
# se usa el prefijo para enrutar también los errores previos a la consulta.
DIRECTORY_CONTEXT = "directory:"


def validate_base_url(raw: str) -> str:
    """Normaliza y valida la URL base de la WebUI.

    Lanza ValueError con un mensaje apto para el usuario. urlsplit lanza
    ValueError con IPv6 mal formada o puertos fuera de rango, de modo que ese
    caso también se traduce aquí en vez de escapar como excepción en la GUI.
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("URL no válida: no puede contener caracteres de control.")
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
    failed = Signal(int, str, object)
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
                self._signals.failed.emit(self.request_id, self._context, exc)
        except Exception:
            if not self._cancelled.is_set():
                self._signals.failed.emit(
                    self.request_id,
                    self._context,
                    "Error inesperado al consultar qBittorrent",
                )
        else:
            if not self._cancelled.is_set():
                self._signals.succeeded.emit(self.request_id, self._context, result)
        finally:
            # Una operación de login captura la contraseña; no conservarla en
            # un QRunnable ya terminado mientras se procesan señales Qt.
            self._operation = None
            self._signals.finished.emit(self.request_id)


class BatchProgressDialog(QDialog):
    """Diálogo modal de progreso del lote; no bloquea el bucle de eventos."""

    cancelled = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        self.label = QLabel("Consultando…")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.cancelled.emit)
        layout.addWidget(cancel, alignment=Qt.AlignmentFlag.AlignRight)

    def set_range(self, maximum: int) -> None:
        self.progress.setRange(0, maximum)

    def set_value(self, value: int) -> None:
        self.progress.setValue(value)

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def reject(self) -> None:
        # Esc o el aspa se interpretan como cancelación cooperativa; el diálogo
        # permanece hasta que termina la consulta en curso.
        self.cancelled.emit()


def _create_settings() -> QSettings:
    """Preferencias propias en INI dentro del perfil del usuario.

    IniFormat/UserScope permite localizar el fichero y aislarlo en pruebas;
    las claves de sistema no se mezclan (``setFallbacksEnabled(False)``).
    """
    settings = QSettings(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORGANIZATION, APPLICATION
    )
    settings.setFallbacksEnabled(False)
    return settings


def _valid_stored_widths(raw, defaults: tuple[int, ...]) -> tuple[int, ...]:
    """Devuelve anchos guardados solo si son una lista completa y plausible.

    Cualquier valor corrupto (texto, longitud distinta, cero, negativo o
    desmesurado) descarta el conjunto y conserva los anchos de fábrica.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != len(defaults):
        return defaults
    widths = []
    for value in raw:
        if isinstance(value, bool):
            return defaults
        if isinstance(value, int):
            width = value
        elif isinstance(value, str):
            try:
                width = int(value.strip())
            except ValueError:
                return defaults
        else:
            return defaults
        if not MIN_COLUMN_WIDTH <= width <= MAX_COLUMN_WIDTH:
            return defaults
        widths.append(width)
    return tuple(widths)


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None, catalog_path=None) -> None:
        super().__init__()
        self._settings = settings if settings is not None else _create_settings()
        self._config_path: Path | None = None
        self._app_config = AppConfig()
        self._config_load_error = ""
        self._setup_dialog: ConnectionDialog | None = None
        self._client: QBittorrentClient | None = None
        self._capabilities = None
        self._setup_client: QBittorrentClient | None = None
        self._setup_capabilities = None
        if hasattr(self._settings, "fileName"):
            self._config_path = Path(self._settings.fileName()).with_suffix(".json")
            if self._config_path.exists():
                try:
                    self._app_config = load_config(self._config_path)
                except ConfigError as exc:
                    self._config_load_error = str(exc)
        initial_url = ""
        if self._app_config.base_url:
            try:
                initial_url = validate_base_url(self._app_config.base_url)
            except ValueError as exc:
                self._config_load_error = str(exc)
                self._app_config = AppConfig()
        self.setWindowTitle("Gestor qBittorrent — revisión local y consulta remota")
        self.setWindowIcon(_load_app_icon())
        self.resize(1000, 780)
        self.busy = False
        self._closing = False
        self._cancel_requested = False
        self._active_id: int | None = None
        self._active_context: str | None = None
        self._request_id = 0
        self._tasks: dict[int, _QueryTask] = {}
        self._batch = False
        self._pending: list[tuple] = []
        self._batch_outcomes: list[tuple] = []
        self._batch_dialog: BatchProgressDialog | None = None
        self._batch_total = 0
        self._directory_cache: dict[str, list[dict]] = {}
        # Grupo propio (no el global) para que la ventana no arrastre trabajos
        # ajenos y para que el cierre no dependa de un waitForDone global.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        central = QWidget()
        root = QVBoxLayout(central)

        local_group = LocalReviewPanel(self._settings, self._directories, self._dir_cache, catalog_path, self._current_server)
        self.local_group = local_group
        self.local_status_label = local_group.status_label
        self.local_table = local_group.table
        self.local_table.horizontalHeader().setStretchLastSection(False)
        self.local_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate(DEFAULT_COLUMN_WIDTHS["local"]):
            self.local_table.setColumnWidth(column, width)
        root.addWidget(local_group)

        remote_group = QGroupBox("qBittorrent — consulta de solo lectura")
        self.remote_group = remote_group
        remote_layout = QVBoxLayout(remote_group)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("URL de la WebUI:"))
        self.url_edit = QLineEdit(initial_url)
        self.url_edit.setPlaceholderText(LOCAL_URL_SUGGESTION)
        self.url_edit.textChanged.connect(self._on_url_changed)
        url_row.addWidget(self.url_edit, 1)
        self.connection_config_button = QPushButton("Configuración")
        self.connection_config_button.setToolTip(str(self._config_path or "Configuración en memoria"))
        self.connection_config_button.clicked.connect(self._open_connection_config)
        url_row.addWidget(self.connection_config_button)
        self.version_button = QPushButton("Consultar versión")
        self.version_button.clicked.connect(self.query_version)
        url_row.addWidget(self.version_button)
        self.cancel_button = QPushButton("Cancelar consulta")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_query)
        url_row.addWidget(self.cancel_button)
        remote_layout.addLayout(url_row)

        # Cabecera común: URL, versión y mensajes de conexión/validación de URL.
        self.connection_label = QLabel(IDLE_CONNECTION_TEXT)
        self.connection_label.setWordWrap(True)
        # Las rutas remotas pueden contener "<" o "&": nunca interpretar HTML.
        self.connection_label.setTextFormat(Qt.TextFormat.PlainText)
        remote_layout.addWidget(self.connection_label)
        self.config_status_label = QLabel()
        self.config_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.config_status_label.setWordWrap(True)
        self.config_status_label.setToolTip(str(self._config_path or "Configuración en memoria"))
        self.config_status_label.hide()
        remote_layout.addWidget(self.config_status_label)
        if self._config_load_error:
            self._show_config_error(
                "No se pudo cargar la configuración. Se conserva el archivo sin modificar. "
                "Corrija o restaure una copia compatible y reinicie la aplicación. "
                + self._config_load_error
            )
        remote_layout.addSpacing(12)

        self.remote_tabs = QTabWidget()
        self.remote_tabs.setObjectName("remoteTabs")
        tab_font = self.remote_tabs.tabBar().font()
        tab_font.setPointSizeF(tab_font.pointSizeF() + 1.5)
        self.remote_tabs.tabBar().setFont(tab_font)
        self.remote_tabs.setStyleSheet(
            "QTabWidget#remoteTabs::pane {"
            " border: 1px solid palette(mid); border-radius: 4px; }"
            "QTabWidget#remoteTabs > QTabBar {"
            f" font-size: {tab_font.pointSizeF():g}pt; }}"
            "QTabWidget#remoteTabs > QTabBar::tab { padding: 8px 16px; }"
            "QTabWidget#remoteTabs > QTabBar::tab:selected {"
            " background: palette(highlight); color: palette(highlighted-text);"
            " font-weight: bold; }"
        )
        remote_layout.addWidget(self.remote_tabs, 1)

        torrents_page = QWidget()
        torrents_layout = QVBoxLayout(torrents_page)
        torrents_row = QHBoxLayout()
        self.torrents_button = QPushButton("Consultar torrents")
        self.torrents_button.clicked.connect(self.query_torrents)
        torrents_row.addWidget(self.torrents_button)
        torrents_row.addStretch(1)
        torrents_layout.addLayout(torrents_row)
        self.torrents_status_label = QLabel(IDLE_TORRENTS_TEXT)
        self.torrents_status_label.setWordWrap(True)
        self.torrents_status_label.setTextFormat(Qt.TextFormat.PlainText)
        torrents_layout.addWidget(self.torrents_status_label)
        self.torrents_table = QTableWidget(0, 4)
        self.torrents_table.setHorizontalHeaderLabels(["Nombre", "Estado", "Progreso", "Destino"])
        self.torrents_table.horizontalHeader().setStretchLastSection(False)
        self.torrents_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate(DEFAULT_COLUMN_WIDTHS["torrents"]):
            self.torrents_table.setColumnWidth(column, width)
        self.torrents_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.torrents_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.torrents_table.setStyleSheet(TABLE_SELECTION_QSS)
        torrents_layout.addWidget(self.torrents_table, 1)
        self.remote_tabs.addTab(torrents_page, "Archivos .torrent")

        directory_page = QWidget()
        directory_layout = QVBoxLayout(directory_page)
        directory_row = QHBoxLayout()
        self.directory_config_button = QPushButton("Configurar directorios")
        self.directory_config_button.clicked.connect(self._open_directory_config)
        directory_row.addWidget(self.directory_config_button)
        self.directory_button = QPushButton("Consultar directorios")
        self.directory_button.clicked.connect(self.query_directories)
        directory_row.addWidget(self.directory_button)
        directory_row.addStretch(1)
        directory_layout.addLayout(directory_row)

        self.directory_status_label = QLabel(IDLE_DIRECTORY_TEXT)
        self.directory_status_label.setWordWrap(True)
        self.directory_status_label.setTextFormat(Qt.TextFormat.PlainText)
        directory_layout.addWidget(self.directory_status_label)

        self.directory_tabs = QTabWidget()
        self.directory_tabs.setObjectName("directoryTabs")
        # Las pestañas de cada carpeta pueden recolocarse arrastrándolas.
        self.directory_tabs.setMovable(True)
        self.directory_tables: dict[str, QTableWidget] = {}
        directory_layout.addWidget(self.directory_tabs, 1)
        self.remote_tabs.addTab(directory_page, "Archivos descargados")
        self._rebuild_directory_tabs()

        root.addWidget(remote_group, 1)
        self.setCentralWidget(central)
        self.table = self.local_table  # compatibilidad con la etapa anterior
        self.statusBar().showMessage(
            "Solo lectura: no se envía ningún torrent, no se vigila nada y no se guardan credenciales"
        )
        self._restore_column_widths()
        self._set_busy(False)
        if self._app_config.connection_mode == "offline":
            self._show_neutral("Modo sin conexión. Puede continuar con la revisión local.")

    # --- anchos de columna persistidos ----------------------------------

    def _table_groups(self) -> dict[str, list[QTableWidget]]:
        return {
            "local": [self.local_table],
            "torrents": [self.torrents_table],
            "directory": list(self.directory_tables.values()),
        }

    def _restore_column_widths(self) -> None:
        """Aplica los anchos guardados; si faltan o no son válidos, los de fábrica."""
        for name, tables in self._table_groups().items():
            defaults = DEFAULT_COLUMN_WIDTHS[name]
            raw = self._settings.value(f"{COLUMN_WIDTHS_GROUP}/{name}")
            if name == "local" and isinstance(raw, (list, tuple)) and len(raw) == 5:
                # Conserva los anchos de la versión anterior y añade solo los nuevos.
                raw = list(_valid_stored_widths(raw, defaults[:5])) + list(defaults[5:])
            stored = _valid_stored_widths(raw, defaults)
            for table in tables:
                for column, width in enumerate(stored):
                    table.setColumnWidth(column, width)

    def _save_column_widths(self) -> bool:
        """Guarda los anchos de las tres tablas y hace visible cualquier fallo.

        No silencia errores: si el fichero no se puede escribir, avisa con un
        diálogo modal y aun así permite cerrar la ventana.
        """
        failed = False
        try:
            for name, tables in self._table_groups().items():
                if not tables:
                    continue
                header = tables[0].horizontalHeader()
                self._settings.setValue(
                    f"{COLUMN_WIDTHS_GROUP}/{name}",
                    [header.sectionSize(column) for column in range(tables[0].columnCount())],
                )
            # sync() es quien escribe de verdad; el estado se consulta después.
            self._settings.sync()
            failed = self._settings.status() != QSettings.Status.NoError
        except Exception:
            # Nunca dejar el cierre a medias por un fallo inesperado del backend.
            failed = True
        if failed:
            QMessageBox.warning(
                self,
                "Anchos de columna no guardados",
                "No se pudieron guardar los anchos de las columnas. La revisión "
                "local y las consultas no se ven afectadas; la ventana se cerrará "
                "igualmente.",
            )
        return not failed

    # --- revisión local -------------------------------------------------

    def _current_server(self) -> str:
        """Servidor activo (URL base normalizada) o '' si no hay conexión.

        Es la clave con la que se asocian directorios y destinos del catálogo;
        cambiar de URL cambia la clave y deja de ofrecer los del servidor anterior.
        """
        if self._app_config.connection_mode == "offline" or not self._app_config.base_url:
            return ""
        try:
            return validate_base_url(self._app_config.base_url)
        except ValueError:
            return ""

    def _directories(self) -> list[DirectoryConfig]:
        """Directorios del servidor activo; los de otros servidores se ocultan."""
        server = self._current_server()
        return [directory for directory in self._app_config.directories
                if directory.server == server]

    def _dir_cache(self) -> dict[str, list[dict]]:
        return self._directory_cache

    def _make_directory_table(self) -> QTableWidget:
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Nombre", "Tipo", "Tamaño"])
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate(DEFAULT_COLUMN_WIDTHS["directory"]):
            table.setColumnWidth(column, width)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setStyleSheet(TABLE_SELECTION_QSS)
        return table

    def _rebuild_directory_tabs(self) -> None:
        self.directory_tabs.clear()
        self.directory_tables = {}
        for directory in self._directories():
            table = self._make_directory_table()
            self.directory_tables[directory.path] = table
            self.directory_tabs.addTab(table, directory.path)
        if not self._directories():
            self._show_neutral(
                "No hay directorios configurados para este servidor. "
                "Abra «Configurar directorios».", DIRECTORY_CONTEXT
            )

    def _show_config_error(self, message: str) -> None:
        self.config_status_label.setText(message)
        self.config_status_label.show()

    def _store_config(self, updated: AppConfig) -> bool:
        if self._config_load_error:
            self._show_config_error(
                "No se puede guardar la configuración: se conserva el archivo original. "
                "Corríjalo o restaure una copia compatible y reinicie la aplicación."
            )
            return False
        if self._config_path is not None:
            try:
                save_config(updated, self._config_path)
            except ConfigError as exc:
                self._show_config_error(f"No se pudo guardar la configuración: {exc}")
                return False
        self._app_config = updated
        self.config_status_label.clear()
        self.config_status_label.hide()
        return True

    def _open_directory_config(self) -> None:
        if self.busy or self._closing or self._config_load_error:
            return
        server = self._current_server()
        current = [directory for directory in self._app_config.directories
                   if directory.server == server]
        dialog = DirectoryConfigDialog(current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # La lista devuelta se asocia al servidor activo; la de otros servidores
        # se conserva intacta.
        updated = [replace(directory, server=server)
                   for directory in (dialog.directories() or [])]
        others = [directory for directory in self._app_config.directories
                  if directory.server != server]
        if not self._store_config(replace(self._app_config, directories=tuple(others + updated))):
            return
        self._directory_cache.clear()
        self._rebuild_directory_tabs()
        self._restore_column_widths()
        self.local_group._render()

    def add_files(self) -> None:
        self.local_group.choose_files()

    def import_files(self, files: list[str]) -> None:
        self.local_group.import_files(files)

    # --- consultas remotas ---------------------------------------------

    def _label_for_context(self, context: str) -> QLabel:
        """Etiqueta de origen de la consulta, no la pestaña visible al responder."""
        if context in {"setup", "setup_access"} and self._setup_dialog is not None:
            return self._setup_dialog.status_label
        if context == "torrents":
            return self.torrents_status_label
        if context.startswith("directory:"):
            return self.directory_status_label
        return self.connection_label

    def _show(self, label: QLabel, style: str, message: str) -> None:
        label.setStyleSheet(style)
        label.setText(message)

    def _show_error(self, message: str, context: str = "version") -> None:
        self._show(self._label_for_context(context), "color: #b00020;", f"Error: {message}")

    def _show_status(self, message: str, context: str = "version") -> None:
        self._show(self._label_for_context(context), "color: #1a7f37;", message)

    def _show_neutral(self, message: str, context: str = "version") -> None:
        self._show(self._label_for_context(context), "", message)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        offline = self._app_config.connection_mode == "offline"
        for button in (self.version_button, self.torrents_button, self.directory_button):
            button.setEnabled(not busy and not offline)
        if self._capabilities is not None:
            self.torrents_button.setEnabled(not busy and not offline and self._capabilities.torrents)
            self.directory_button.setEnabled(not busy and not offline and self._capabilities.directories)
        self.directory_config_button.setEnabled(not busy and not self._config_load_error)
        self.connection_config_button.setEnabled(not busy)
        # Tras pedir la cancelación el botón se apaga, pero la ventana sigue
        # ocupada hasta que el hilo termina: nunca hay dos trabajos a la vez.
        self.cancel_button.setEnabled(busy and not self._cancel_requested)
        self.url_edit.setEnabled(not busy and not offline)
        if self._setup_dialog is not None:
            self._setup_dialog.set_busy(busy)

    def _persist_url(self, raw: str) -> bool:
        """Guarda la URL si es válida; una URL incompleta o inválida no se escribe."""
        try:
            normalized = validate_base_url(raw)
        except ValueError:
            return False
        if normalized == self._app_config.base_url:
            return True
        updated = replace(self._app_config, base_url=normalized)
        return self._store_config(updated)

    def _reset_connection(self) -> None:
        if self._client is not None:
            self._client.clear_session()
        self._client = None
        self._capabilities = None
        self.local_group.set_remote_torrents(None)
        self.torrents_table.setRowCount(0)
        self._directory_cache.clear()
        for table in self.directory_tables.values():
            table.setRowCount(0)
        self._show_neutral(IDLE_TORRENTS_TEXT, "torrents")
        self._show_neutral(IDLE_DIRECTORY_TEXT, DIRECTORY_CONTEXT)

    def _on_url_changed(self, text: str) -> None:
        """Cambiar de servidor invalida los datos del anterior."""
        if self._closing:
            return
        had_rows = bool(self.torrents_table.rowCount() or any(
            table.rowCount() for table in self.directory_tables.values()
        ))
        self._persist_url(text)
        self._reset_connection()
        self._rebuild_directory_tabs()
        self._restore_column_widths()
        self._set_busy(self.busy)
        if self.busy:
            self.cancel_query()
            return
        if had_rows:
            self._show_neutral(
                "URL cambiada: se han descartado los resultados del servidor anterior. "
                "Consulte de nuevo en la pestaña correspondiente."
            )
        else:
            self._show_neutral(IDLE_CONNECTION_TEXT)

    def _client_for_url(self, base_url: str) -> QBittorrentClient:
        if self._client is None or self._client.base_url != base_url:
            if self._client is not None:
                self._client.clear_session()
            self._client = QBittorrentClient(base_url, timeout=QUERY_TIMEOUT)
            self._capabilities = None
        return self._client

    def _discard_setup_session(self) -> None:
        if self._setup_client is not None:
            self._setup_client.clear_session()
        self._setup_client = None
        self._setup_capabilities = None

    def _check_access(self) -> None:
        dialog = self._setup_dialog
        if (dialog is None or self.busy or self._closing
                or dialog.mode_combo.currentData() == "offline"):
            return
        self._discard_setup_session()
        try:
            url = validate_base_url(dialog.url_edit.text())
        except ValueError as exc:
            dialog.status_label.setText(str(exc))
            return
        use_auth = dialog.auth_check.isChecked()
        username, password = dialog.username_edit.text(), dialog.password_edit.text()
        if use_auth and (not username or not password):
            dialog.status_label.setText("Indique usuario y contraseña para comprobar el acceso.")
            return
        candidate = QBittorrentClient(url, timeout=QUERY_TIMEOUT)
        self._setup_client = candidate
        # El formulario no retiene la contraseña tras iniciar la petición.
        dialog.password_edit.blockSignals(True)
        try:
            dialog.password_edit.clear()
        finally:
            dialog.password_edit.blockSignals(False)

        def check(client):
            if use_auth:
                client.login(username, password)
            return client.capabilities()

        self._dispatch_query(check, "setup_access", "Comprobando acceso y funciones…", candidate)

    def _start_query(self, operation, context: str, busy_text: str) -> None:
        if self.busy or self._closing or self._app_config.connection_mode == "offline":
            return
        try:
            base_url = validate_base_url(self.url_edit.text())
        except ValueError as exc:
            self._show_error(str(exc), context)
            return
        client = self._client_for_url(base_url)
        self._dispatch_query(operation, context, busy_text, client)

    def _dispatch_query(self, operation, context: str, busy_text: str,
                        client: QBittorrentClient) -> None:
        self._request_id += 1
        request_id = self._request_id
        signals = _QuerySignals()
        signals.succeeded.connect(self._on_success)
        signals.failed.connect(self._on_failure)
        signals.finished.connect(self._on_finished)
        task = _QueryTask(request_id, lambda: operation(client), context, signals)
        self._tasks[request_id] = task
        self._active_id = request_id
        # El contexto se captura al iniciar: los mensajes posteriores vuelven a
        # la pestaña de origen aunque el usuario cambie de pestaña entretanto.
        self._active_context = context
        self._cancel_requested = False
        self._set_busy(True)
        self._show_status(busy_text, context)
        self._pool.start(task)

    def query_version(self) -> None:
        self._start_query(
            lambda client: client.version(), "version", "Consultando versión de qBittorrent…"
        )

    def query_torrents(self) -> None:
        if self._capabilities is not None and not self._capabilities.torrents:
            self._show_error("La Web API no es compatible con esta consulta.", "torrents")
            return
        self._start_query(
            lambda client: client.torrents_info(include_files=False),
            "torrents",
            "Consultando torrents (solo lectura)…",
        )

    def query_directories(self) -> None:
        if self._capabilities is not None and not self._capabilities.directories:
            self._show_error(
                "La Web API no es compatible con el listado de directorios con metadatos.",
                DIRECTORY_CONTEXT,
            )
            return
        if self.busy or self._closing or self._app_config.connection_mode == "offline":
            return
        try:
            validate_base_url(self.url_edit.text())
        except ValueError as exc:
            self._show_error(str(exc), DIRECTORY_CONTEXT)
            return
        paths = [directory.path for directory in self._directories()]
        if not paths:
            self._show_error(
                "No hay directorios configurados para este servidor. "
                "Abra «Configurar directorios».", DIRECTORY_CONTEXT
            )
            return
        items = [
            (f"directory:{path}",
             (lambda client, path=path: client.directory_content(path, with_metadata=True)),
             f"Consultando el nivel inmediato de {path}…")
            for path in paths
        ]
        self._run_batch(items, "Consultar directorios")

    def _open_connection_config(self) -> None:
        if self.busy or self._closing:
            return
        if self._setup_dialog is None or not self._setup_dialog.isVisible():
            if self._setup_dialog is not None:
                self._setup_dialog.deleteLater()
            self._setup_dialog = ConnectionDialog(self._app_config, self)
            self._setup_dialog.save_button.clicked.connect(self._save_connection_config)
            self._setup_dialog.test_button.clicked.connect(self._test_connection)
            self._setup_dialog.access_button.clicked.connect(self._check_access)
            for signal in (
                self._setup_dialog.url_edit.textChanged,
                self._setup_dialog.mode_combo.currentIndexChanged,
                self._setup_dialog.auth_check.toggled,
                self._setup_dialog.username_edit.textChanged,
                self._setup_dialog.password_edit.textChanged,
            ):
                signal.connect(self._discard_setup_session)
            self._setup_dialog.rejected.connect(self._cancel_connection_test)
        self._setup_dialog.show()
        self._setup_dialog.raise_()

    def _test_connection(self) -> None:
        dialog = self._setup_dialog
        if (dialog is None or self.busy or self._closing
                or dialog.mode_combo.currentData() == "offline"):
            return
        try:
            url = validate_base_url(dialog.url_edit.text())
        except ValueError as exc:
            dialog.status_label.setText(str(exc))
            return
        self._dispatch_query(lambda client: client.version(), "setup",
                             "Comprobando conexión…",
                             QBittorrentClient(url, timeout=QUERY_TIMEOUT))

    def _cancel_connection_test(self) -> None:
        if self._active_context in {"setup", "setup_access"}:
            self.cancel_query()
        elif not self.busy:
            self._discard_setup_session()

    def _save_connection_config(self) -> None:
        dialog = self._setup_dialog
        if dialog is None or self.busy or self._closing:
            return
        mode = dialog.mode_combo.currentData()
        if (mode != "offline" and dialog.auth_check.isChecked()
                and self._setup_capabilities is None):
            dialog.status_label.setText(
                "Debe comprobar el acceso con sus credenciales antes de guardar."
            )
            return
        try:
            url = (self._app_config.base_url if mode == "offline"
                   else validate_base_url(dialog.url_edit.text()))
        except ValueError as exc:
            dialog.status_label.setText(str(exc))
            return
        updated = replace(self._app_config, base_url=url, connection_mode=mode,
                          setup_completed=True, query_on_startup=dialog.startup_check.isChecked())
        # El valor ajustado se recuerda como defecto de ese modo, para que
        # reaparezca la próxima vez que se elija.
        if mode == "local":
            updated = replace(updated, local_url=url)
        elif mode == "remote":
            updated = replace(updated, remote_url=url)
        if not self._store_config(updated):
            dialog.status_label.setText(self.config_status_label.text())
            return
        if (url != self.url_edit.text() or mode == "offline"
                or self._setup_capabilities is not None):
            self._reset_connection()
        self._rebuild_directory_tabs()
        self.local_group.refresh()
        self._restore_column_widths()
        if (self._setup_client is not None and self._setup_capabilities is not None
                and mode != "offline" and self._setup_client.base_url == url):
            self._client = self._setup_client
            self._capabilities = self._setup_capabilities
            self._setup_client = None
            self._setup_capabilities = None
        # Sincroniza la cabecera sin repetir el guardado ni los efectos de
        # cambio de servidor: la configuración ya quedó persistida.
        self.url_edit.blockSignals(True)
        try:
            self.url_edit.setText(url)
        finally:
            self.url_edit.blockSignals(False)
        self._set_busy(False)
        self._show_neutral(
            "Modo sin conexión. Puede continuar con la revisión local." if mode == "offline" else
            "Configuración guardada. Consulte desde una pestaña cuando lo desee."
        )
        dialog.accept()

    def startup_query(self) -> None:
        """Configura el primer inicio; solo consulta una conexión confirmada."""
        if self.busy or self._closing:
            return
        if self._config_load_error:
            return
        if not self._app_config.setup_completed:
            self._open_connection_config()
            return
        if self._app_config.connection_mode == "offline" or not self._app_config.query_on_startup:
            return
        try:
            validate_base_url(self.url_edit.text())
        except ValueError:
            return
        items = [(
            "torrents",
            lambda client: client.torrents_info(include_files=False),
            "Consultando torrents (solo lectura)…",
        )]
        for directory in self._directories():
            path = directory.path
            items.append((
                f"directory:{path}",
                (lambda client, path=path: client.directory_content(path, with_metadata=True)),
                f"Consultando el nivel inmediato de {path}…",
            ))
        self._run_batch(items, "Consulta inicial")

    def _run_batch(self, items: list[tuple], title: str) -> None:
        self._batch = True
        self._pending = list(items)
        self._batch_outcomes = []
        self._batch_total = len(items)
        dialog = BatchProgressDialog(title, self)
        dialog.set_range(self._batch_total)
        dialog.cancelled.connect(self.cancel_query)
        self._batch_dialog = dialog
        dialog.show()
        self._start_next_batch_query()

    def _start_next_batch_query(self) -> None:
        if not self._pending:
            self._finish_batch()
            return
        context, operation, busy_text = self._pending[0]
        self._pending = self._pending[1:]
        try:
            base_url = validate_base_url(self.url_edit.text())
        except ValueError as exc:
            self._record_batch_outcome(False, context, str(exc))
            self._pending = []
            self._finish_batch()
            return
        client = self._client_for_url(base_url)
        if self._capabilities is not None and (
            (context == "torrents" and not self._capabilities.torrents)
            or (context.startswith(DIRECTORY_CONTEXT) and not self._capabilities.directories)
        ):
            message = "Consulta omitida: función no compatible con la Web API comprobada."
            self._show_error(message, context)
            self._record_batch_outcome(False, context, message)
            self._start_next_batch_query()
            return
        if self._batch_dialog is not None:
            self._batch_dialog.set_text(busy_text)
        self._dispatch_query(operation, context, busy_text, client)

    def _record_batch_outcome(self, ok: bool, context: str, text: str) -> None:
        if self._batch:
            self._batch_outcomes.append((ok, context, text))

    def _batch_item_label(self, context: str) -> str:
        if context == "torrents":
            return "Torrents"
        if context.startswith("directory:"):
            return f"Directorio {context.split(':', 1)[1]}"
        return context

    def _aggregate_batch_outcomes(self, outcomes: list[tuple]) -> list[str]:
        ok_lines = []
        error_contexts: dict[str, list[str]] = {}
        for ok, context, text in outcomes:
            if ok:
                ok_lines.append(f"{self._batch_item_label(context)}: {text}")
            else:
                error_contexts.setdefault(text, []).append(context)
        error_lines = []
        for text, contexts in error_contexts.items():
            if len(contexts) == 1:
                error_lines.append(f"{self._batch_item_label(contexts[0])}: {text}")
            else:
                error_lines.append(f"{text} ({len(contexts)} consultas afectadas)")
        return ok_lines + error_lines

    def _finish_batch(self, cancelled: bool = False) -> None:
        dialog = self._batch_dialog
        self._batch_dialog = None
        self._batch = False
        self._pending = []
        self._set_busy(False)
        if dialog is not None:
            dialog.hide()
            dialog.deleteLater()
        if cancelled:
            return
        outcomes = self._batch_outcomes
        self._batch_outcomes = []
        if not outcomes:
            return
        lines = self._aggregate_batch_outcomes(outcomes)
        text = "\n".join(lines)
        if any(not ok for ok, _context, _text in outcomes):
            QMessageBox.warning(self, "Consulta completada con errores", text)
        else:
            QMessageBox.information(self, "Consulta completada", text)

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
            "consulta hasta que termine la actual.",
            self._active_context or "version",
        )

    @Slot(int, str, object)
    def _on_success(self, request_id: int, context: str, payload) -> None:
        if self._closing or self._cancel_requested or request_id != self._active_id:
            return
        if context == "setup_access":
            self._setup_capabilities = payload
            access = "Sesión autenticada" if self._setup_client.authenticated else "Acceso sin credenciales"
            self._show_status(
                f"{access}. qBittorrent {payload.version}; Web API {payload.webapi_version}. "
                f"Torrents: {'compatible' if payload.torrents else 'no compatible'}. "
                f"Directorios: {'compatible' if payload.directories else 'no compatible'}. "
                "Compatibilidad según versión; no comprueba permisos de carpetas ni de escritura. "
                "Pulse Guardar para aplicar esta conexión.", context,
            )
        elif context in {"version", "setup"}:
            self._show_status(f"Conectado. Versión de qBittorrent: {payload}", context)
        elif context == "torrents":
            self._fill_torrents(payload)
            self._show_status(
                f"{len(payload)} torrent(s) recibidos en modo solo lectura.", context
            )
            self._record_batch_outcome(True, context, f"{len(payload)} torrent(s)")
        elif context.startswith("directory:"):
            path = context.split(":", 1)[1]
            self._directory_cache[path] = payload
            self._fill_directory(path, payload)
            self._show_status(
                f"{len(payload)} entrada(s) en {path} (solo el nivel inmediato).", context
            )
            self._record_batch_outcome(True, context, f"{len(payload)} entrada(s)")

    @Slot(int, str, object)
    def _on_failure(self, request_id: int, context: str, error) -> None:
        if self._closing or self._cancel_requested or request_id != self._active_id:
            return
        message = str(error)
        if context == "setup_access":
            self._discard_setup_session()
        elif context != "setup" and getattr(error, "status_code", None) in {401, 403}:
            if self._client is not None:
                self._client.clear_session()
            self._capabilities = None
            self._pending = []
            message += (
                " Abra Configuración para comprobar el acceso o iniciar sesión de nuevo. "
                "No se reintenta automáticamente."
            )
            self._show_error(
                "Acceso rechazado o sesión caducada. " + message
            )
        self._record_batch_outcome(False, context, message)
        rows = 0
        if context == "torrents":
            rows = self.torrents_table.rowCount()
        elif context.startswith("directory:"):
            path = context.split(":", 1)[1]
            table = self.directory_tables.get(path)
            rows = table.rowCount() if table is not None else 0
        if rows:
            message += " Se mantienen los últimos datos mostrados."
        self._show_error(message, context)
        if not self._batch and context == "torrents":
            QMessageBox.warning(self, "Error al consultar torrents", message)

    @Slot(int)
    def _on_finished(self, request_id: int) -> None:
        self._tasks.pop(request_id, None)
        if request_id != self._active_id:
            return
        cancelled = self._cancel_requested
        context = self._active_context or "version"
        self._active_id = None
        self._active_context = None
        self._cancel_requested = False
        self._set_busy(False)
        if cancelled and context in {"setup", "setup_access"}:
            self._discard_setup_session()
        if self._closing:
            # Cierre diferido: aceptar ahora que no quedan trabajos.
            if self._batch_dialog is not None:
                self._batch_dialog.hide()
                self._batch_dialog.deleteLater()
                self._batch_dialog = None
            self._show_neutral("Consulta finalizada; cerrando la ventana.")
            if not self._tasks:
                self.close()
            return
        if self._batch:
            if cancelled:
                self._pending = []
            if self._batch_dialog is not None:
                self._batch_dialog.set_value(self._batch_total - len(self._pending))
            if self._pending:
                self._start_next_batch_query()
                return
            self._finish_batch(cancelled)
            return
        if cancelled:
            self._show_neutral(
                "Consulta cancelada; no se muestran datos de esa consulta.", context
            )

    def _fill_torrents(self, torrents: list[dict]) -> None:
        self.local_group.set_remote_torrents(torrents)
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

    def _fill_directory(self, path: str, entries: list[dict]) -> None:
        table = self.directory_tables.get(path)
        if table is None:
            return
        table.setRowCount(0)
        for entry in entries:
            row = table.rowCount()
            table.insertRow(row)
            is_dir = entry.get("type") == "dir"
            values = [
                entry.get("name", "—"),
                "Carpeta" if is_dir else "Archivo",
                "—" if is_dir else _format_size(entry.get("size")),
            ]
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))

    def closeEvent(self, event) -> None:
        # Ceder foco consolida la celda/combobox activa antes del cierre del catálogo.
        if not self._closing and not self.local_group.commit_edits():
            event.ignore()  # No ocultar un fallo de guardado al cerrar el editor activo.
            return
        self._closing = True
        if self._tasks:
            # Sin waitForDone: se ignora el cierre, se cancela (marca
            # cooperativa, nunca terminate) y se cierra al llegar «finished».
            if self._batch_dialog is not None:
                self._batch_dialog.hide()
                self._batch_dialog.deleteLater()
                self._batch_dialog = None
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
        if self._client is not None:
            self._client.clear_session()
        self._client = None
        self._capabilities = None
        self._discard_setup_session()
        if self._setup_dialog is not None:
            self._setup_dialog.clear_credentials()
        # Solo en el cierre efectivo (sin trabajos pendientes), también cuando
        # se llega aquí desde el cierre diferido de _on_finished.
        self._save_column_widths()
        self.local_group.close_catalog()
        event.accept()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    # El estilo nativo de Windows dibuja los QComboBox embebidos en la tabla con
    # un texto fantasma (doble render). Fusion es consistente y lo evita.
    app.setStyle("Fusion")
    app.setWindowIcon(_load_app_icon())
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window.startup_query)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
