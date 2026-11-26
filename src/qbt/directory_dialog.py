"""Diálogo de configuración de directorios de descarga."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import DIRECTORY_KINDS, ConfigError, DirectoryConfig, validate_directories


class DirectoryConfigDialog(QDialog):
    """Edita la lista de directorios (ruta absoluta + tipo) con validación al aceptar."""

    def __init__(self, directories, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configurar directorios de descarga")
        self.resize(700, 420)
        self._result: list[DirectoryConfig] | None = None

        layout = QVBoxLayout(self)

        hint = QLabel(
            "Rutas absolutas tal como las ve qBittorrent (p. ej. C:\\Descargas o /home/usuario/Descargas). "
            "Solo se consulta el nivel inmediato; no se hace lectura recursiva."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Ruta", "Tipo"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(0, 480)
        self.table.setColumnWidth(1, 140)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        layout.addWidget(self.table, 1)

        row_buttons = QHBoxLayout()
        self.add_button = QPushButton("Añadir")
        self.add_button.clicked.connect(self._add_row)
        self.remove_button = QPushButton("Quitar selección")
        self.remove_button.clicked.connect(self._remove_rows)
        row_buttons.addWidget(self.add_button)
        row_buttons.addWidget(self.remove_button)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)

        confirm = QHBoxLayout()
        confirm.addStretch(1)
        ok_button = QPushButton("Aceptar")
        ok_button.setDefault(True)
        ok_button.clicked.connect(self._accept)
        cancel_button = QPushButton("Cancelar")
        cancel_button.clicked.connect(self.reject)
        confirm.addWidget(ok_button)
        confirm.addWidget(cancel_button)
        layout.addLayout(confirm)

        for directory in directories:
            self._append_row(directory.path, directory.kind)

    def _append_row(self, path: str, kind: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(path))
        combo = QComboBox()
        combo.addItems(DIRECTORY_KINDS)
        combo.setCurrentText(kind if kind in DIRECTORY_KINDS else DIRECTORY_KINDS[0])
        self.table.setCellWidget(row, 1, combo)

    def _add_row(self) -> None:
        self._append_row("", DIRECTORY_KINDS[0])
        last = self.table.rowCount() - 1
        self.table.setCurrentCell(last, 0)
        self.table.editItem(self.table.item(last, 0))

    def _remove_rows(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _collect(self) -> list[DirectoryConfig]:
        result = []
        for row in range(self.table.rowCount()):
            path_item = self.table.item(row, 0)
            combo = self.table.cellWidget(row, 1)
            path = path_item.text() if path_item is not None else ""
            kind = combo.currentText() if combo is not None else DIRECTORY_KINDS[0]
            result.append(DirectoryConfig(path, kind))
        return result

    def _accept(self) -> None:
        try:
            self._result = list(validate_directories(self._collect()))
        except ConfigError as exc:
            QMessageBox.warning(self, "Directorios no válidos", str(exc))
            return
        self.accept()

    def directories(self) -> list[DirectoryConfig] | None:
        """Devuelve la lista validada tras aceptar; None si se canceló."""
        return self._result
