"""Configuración de conexión sin efectos de red ni persistencia implícitos."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

LOCAL_URL_SUGGESTION = "http://localhost:8080"


class ConnectionDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configurar qBittorrent")
        self.setMinimumWidth(580)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Elija dónde se ejecuta qBittorrent. Puede configurar la conexión más tarde "
            "y seguir revisando archivos .torrent sin conexión."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.mode_combo = QComboBox()
        for label, value in (
            ("En este ordenador", "local"),
            ("En otro ordenador o contenedor", "remote"),
            ("Continuar sin conexión", "offline"),
        ):
            self.mode_combo.addItem(label, value)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(config.connection_mode))
        layout.addWidget(self.mode_combo)
        layout.addWidget(QLabel("URL de la WebUI de qBittorrent:"))
        self.url_edit = QLineEdit(config.base_url or (
            LOCAL_URL_SUGGESTION if config.connection_mode == "local" else ""
        ))
        self.url_edit.setPlaceholderText("http(s)://servidor:puerto")
        layout.addWidget(self.url_edit)
        hint = QLabel(
            "localhost:8080 es solo una sugerencia: compruebe la dirección y el puerto "
            "de la WebUI. Esta fase todavía no permite introducir usuario, contraseña "
            "ni clave API. No desactive la autenticación para utilizar la aplicación; "
            "si el servidor la exige, puede continuar con la revisión local."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.startup_check = QCheckBox("Consultar automáticamente al iniciar")
        self.startup_check.setChecked(config.query_on_startup)
        layout.addWidget(self.startup_check)
        self.status_label = QLabel("No se conecta ni se guarda nada hasta que lo indique.")
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.test_button = QPushButton("Probar conexión")
        self.save_button = QPushButton("Guardar")
        self.later_button = QPushButton("Configurar más tarde")
        self.later_button.clicked.connect(self.reject)
        for button in (self.test_button, self.save_button, self.later_button):
            # Intro en la URL no debe activar una consulta o guardar el borrador.
            button.setAutoDefault(False)
            button.setDefault(False)
            row.addWidget(button)
        layout.addLayout(row)
        self._busy = False
        self.mode_combo.currentIndexChanged.connect(self._update_mode)
        self.mode_combo.currentIndexChanged.connect(self._clear_probe_result)
        self.url_edit.textChanged.connect(self._clear_probe_result)
        self._update_mode()

    def _clear_probe_result(self):
        self.status_label.setStyleSheet("")
        self.status_label.setText("Configuración modificada; conexión pendiente de comprobar.")

    def _update_mode(self):
        offline = self.mode_combo.currentData() == "offline"
        self.url_edit.setEnabled(not self._busy and not offline)
        self.startup_check.setEnabled(not self._busy and not offline)
        self.test_button.setEnabled(not self._busy and not offline)

    def set_busy(self, busy):
        self._busy = busy
        self.mode_combo.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self._update_mode()
