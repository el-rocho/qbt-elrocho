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
            "de la WebUI. La sesión del navegador no se comparte. No desactive la "
            "autenticación del servidor; puede introducir sus credenciales aquí "
            "o continuar con la revisión local. HTTP no cifra las credenciales: "
            "utilice HTTPS fuera de un entorno de confianza."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.auth_check = QCheckBox("Usar usuario y contraseña de qBittorrent")
        layout.addWidget(self.auth_check)
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("Usuario de qBittorrent")
        self.username_edit.setAccessibleName("Usuario de qBittorrent")
        layout.addWidget(self.username_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Contraseña (solo en memoria)")
        self.password_edit.setAccessibleName("Contraseña de qBittorrent")
        layout.addWidget(self.password_edit)
        self.access_button = QPushButton("Comprobar acceso y funciones")
        self.access_button.setAutoDefault(False)
        self.access_button.setDefault(False)
        layout.addWidget(self.access_button)
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
        self.auth_check.toggled.connect(self._update_mode)
        self.auth_check.toggled.connect(self._clear_probe_result)
        self.username_edit.textChanged.connect(self._clear_probe_result)
        self.password_edit.textChanged.connect(self._clear_probe_result)
        self.finished.connect(self.clear_credentials)
        self._update_mode()

    def clear_credentials(self):
        self.username_edit.clear()
        self.password_edit.clear()

    def _clear_probe_result(self):
        self.status_label.setStyleSheet("")
        self.status_label.setText("Configuración modificada; conexión pendiente de comprobar.")

    def _update_mode(self):
        offline = self.mode_combo.currentData() == "offline"
        self.url_edit.setEnabled(not self._busy and not offline)
        self.startup_check.setEnabled(not self._busy and not offline)
        self.test_button.setEnabled(not self._busy and not offline)
        self.access_button.setEnabled(not self._busy and not offline)
        self.auth_check.setEnabled(not self._busy and not offline)
        auth = self.auth_check.isChecked()
        self.username_edit.setEnabled(not self._busy and not offline and auth)
        self.password_edit.setEnabled(not self._busy and not offline and auth)

    def set_busy(self, busy):
        self._busy = busy
        self.mode_combo.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self._update_mode()
