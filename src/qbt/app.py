"""Primera interfaz local: importar y revisar propuestas, sin integración de red."""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from .classification import classify


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Gestor qBittorrent — revisión local")
        self.resize(900, 420)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Archivo", "Tipo", "Título", "Temporada", "Confianza"])
        self.table.horizontalHeader().setStretchLastSection(True)
        add_button = QPushButton("Añadir archivos .torrent…")
        add_button.clicked.connect(self.add_files)
        layout = QVBoxLayout()
        layout.addWidget(add_button)
        layout.addWidget(self.table)
        widget = QWidget()
        widget.setLayout(layout)
        self.setCentralWidget(widget)
        self.statusBar().showMessage("Modo local: no hay conexión ni envío a qBittorrent")

    def add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Seleccionar torrents", "", "Archivos torrent (*.torrent)")
        for filename in files:
            proposal = classify(filename)
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [proposal.source.name, proposal.kind, proposal.title,
                      f"{proposal.season:02d}" if proposal.season is not None else "—", proposal.confidence]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
