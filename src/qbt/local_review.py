"""Panel de revisión y catálogo local; nunca escribe en el servidor remoto."""

from dataclasses import asdict, replace
from pathlib import Path, PureWindowsPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QGroupBox, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .catalog import Catalog, CatalogError, LocalRecord
from .classification import classify
from .torrent import TorrentError, read_torrent
from .review import find_matches

KINDS = ("Sin determinar", "Película", "Serie", "Libro")
KIND_PATHS = {"Película": "peliculas", "Serie": "series", "Libro": "libros"}


def _valid_destination(value: str) -> bool:
    return not value or (not any(ord(char) < 32 for char in value) and (
        value.startswith("/") or PureWindowsPath(value).is_absolute()
    ))


class LocalReviewPanel(QGroupBox):
    def __init__(self, settings, roots, catalog_path=None) -> None:
        super().__init__("Revisión local — catálogo y destinos (sin envío)")
        self._roots = roots
        self._rendering = False
        self._last_save_error: str | None = None
        self._records: list[LocalRecord] = []
        self._remote_torrents: list[dict] | None = None
        self._catalog = None
        layout = QVBoxLayout(self)
        self.add_button = QPushButton("Añadir archivos .torrent…")
        self.add_button.clicked.connect(self.choose_files)
        layout.addWidget(self.add_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Archivo", "Tipo", "Título", "Temporada", "Confianza", "Destino", "Incluir", "Avisos"]
        )
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.itemChanged.connect(self._item_changed)
        layout.addWidget(self.table)
        if catalog_path is None:
            catalog_path = (Path(settings.fileName()).with_suffix(".sqlite3")
                            if hasattr(settings, "fileName") else ":memory:")
        try:
            self._catalog = Catalog(catalog_path)
            self._records = self._catalog.list_records()
        except (CatalogError, OSError) as exc:
            if self._catalog is not None:
                self._catalog.close()
                self._catalog = None
            self.add_button.setEnabled(False)
            self._error(f"No se pudo abrir el catálogo. No se sobrescribe: {exc}")
        else:
            self.status_label.setToolTip(str(catalog_path))
            self._summary()
            self._verify_sources()
        self._render()

    def _verify_sources(self) -> None:
        for record in list(self._records):
            try:
                identity = read_torrent(record.source)
            except TorrentError as exc:
                error = str(exc)
            else:
                error = ("El archivo ha cambiado; vuelva a añadirlo para revisar su nueva identidad."
                         if (identity.info_hash_v1, identity.info_hash_v2)
                         != (record.info_hash_v1, record.info_hash_v2) else "")
            updated = replace(record, error=error, included=False if error else record.included)
            if updated != record and not self._save(updated):
                break

    def _error(self, message: str) -> None:
        self.status_label.setStyleSheet("color: #b00020;")
        self.status_label.setText(message)

    def _reject(self, message: str) -> None:
        """Rechazo de validación con el mismo aviso persistente que un fallo de guardado.

        Se registra en _last_save_error para que el cierre de la ventana y el
        resumen posterior a una consulta remota no oculten que el dato sigue
        sin guardarse.
        """
        self._last_save_error = message
        self._error(message)

    def _summary(self) -> None:
        if self._last_save_error:
            self._error(self._last_save_error)
            return
        self.status_label.setStyleSheet("")
        self.status_label.setText(
            f"{len(self._records)} revisión(es) guardada(s) localmente. "
            "Edite tipo, título, temporada y destino; Incluir no envía nada. "
            "Las rutas dependen de cada usuario; no se crean subcarpetas. "
            + ("Sin consulta remota: no se conoce el estado actual del servidor."
               if self._remote_torrents is None else
               "Comparación con la última consulta remota, temporal; no se actualiza sola.")
        )

    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Seleccionar torrents", "", "Archivos torrent (*.torrent)"
        )
        self.import_files(files)

    def _destination_options(self, kind: str) -> list[str]:
        options = [self._roots().get(KIND_PATHS.get(kind, ""), ""),
                   *[r.destination for r in self._records if r.kind == kind and r.destination]]
        return list(dict.fromkeys(option for option in options if option and _valid_destination(option)))

    def _proposed_destination(self, kind: str) -> str:
        options = self._destination_options(kind)
        if kind == "Serie" and len(options) > 1:
            return ""  # Varias raíces: no decidir por el usuario dónde conservar la serie.
        proposed = self._roots().get(KIND_PATHS.get(kind, ""), "")
        return proposed if _valid_destination(proposed) else ""

    def import_files(self, files: list[str]) -> None:
        if self._catalog is None:
            return
        for filename in files:
            source = str(Path(filename).expanduser().resolve())
            previous = next((r for r in self._records if r.source == source), None)
            try:
                identity = read_torrent(source)
            except TorrentError as exc:
                if previous is not None:
                    # Fuente ilegible o corrupta: se conservan las correcciones
                    # manuales y la última identidad conocida; solo se marca el
                    # error y se excluye. No se recalcula la propuesta hasta
                    # volver a leer un torrent válido.
                    if not self._save(replace(previous, error=str(exc), included=False)):
                        break
                    continue
                proposal = classify(source)
                v1, v2, error = None, None, str(exc)
            else:
                if previous is not None and (identity.info_hash_v1, identity.info_hash_v2) == (
                    previous.info_hash_v1, previous.info_hash_v2
                ):
                    if previous.error:
                        self._save(replace(previous, error="", included=False))
                    continue  # Reimportar la misma identidad conserva las correcciones.
                proposal = classify(source)
                v1, v2, error = identity.info_hash_v1, identity.info_hash_v2, ""
            destination = self._proposed_destination(proposal.kind)
            record = LocalRecord(source, v1, v2, proposal.kind, proposal.title,
                                 proposal.season, proposal.quality, proposal.confidence,
                                 destination, not error and previous is None, error)
            exact, _similar = find_matches(v1, v2, record.title,
                                            [asdict(r) for r in self._records if r.source != source]
                                            + (self._remote_torrents or []))
            if exact:
                record = replace(record, included=False)
            if not self._save(record):
                break
        self._render()

    def _save(self, record: LocalRecord) -> bool:
        try:
            self._catalog.save(record)
        except CatalogError as exc:
            self._last_save_error = f"No se guardó la revisión; se conserva el último valor guardado. {exc}"
            self._error(self._last_save_error)
            self._render()
            return False
        self._last_save_error = None
        for index, previous in enumerate(self._records):
            if previous.source == record.source:
                self._records[index] = record
                break
        else:
            self._records.append(record)
        self._summary()
        return True

    def _warnings(self, record: LocalRecord) -> str:
        if record.error:
            return record.error
        exact, similar = find_matches(
            record.info_hash_v1, record.info_hash_v2, record.title,
            [asdict(r) for r in self._records if r.source != record.source],
        )
        remote_exact, remote_similar = find_matches(
            record.info_hash_v1, record.info_hash_v2, record.title,
            self._remote_torrents or [],
        )
        messages = []
        if remote_exact:
            messages.append("Torrent idéntico en el servidor remoto consultado")
        if remote_similar:
            messages.append("Título parecido en el servidor remoto; revisar temporada y calidad")
        if exact:
            messages.append("Torrent idéntico en el catálogo local")
        if similar:
            messages.append("Título parecido en el catálogo; revisar temporada y calidad")
        if record.kind == "Sin determinar":
            messages.append("Tipo pendiente de revisión")
        if not record.destination:
            messages.append("Elegir destino")
        return "; ".join(messages)

    def _render(self) -> None:
        self._rendering = True
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._records))
            for row, record in enumerate(self._records):
                values = [Path(record.source).name, record.kind, record.title,
                          f"{record.season:02d}" if record.season is not None else "—",
                          record.confidence, record.destination, "", self._warnings(record)]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column not in (2, 3):
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if column == 6:
                        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                        item.setCheckState(Qt.Checked if record.included else Qt.Unchecked)
                    item.setToolTip(value if column else record.source)
                    self.table.setItem(row, column, item)
                kind = QComboBox()
                kind.addItems(KINDS)
                kind.setCurrentText(record.kind)
                kind.currentTextChanged.connect(
                    lambda text, source=record.source: self._change(source, kind=text)
                )
                self.table.setCellWidget(row, 1, kind)
                destination = QComboBox()
                destination.setEditable(True)
                destination.setInsertPolicy(QComboBox.NoInsert)
                choices = self._destination_options(record.kind)
                destination.addItems([choice for choice in choices if choice])
                destination.setEditText(record.destination)
                destination.activated.connect(
                    lambda _index, source=record.source, combo=destination:
                    self._change(source, destination=combo.currentText().strip())
                )
                destination.lineEdit().editingFinished.connect(
                    lambda source=record.source, combo=destination:
                    self._change(source, destination=combo.currentText().strip())
                )
                self.table.setCellWidget(row, 5, destination)
            self.table.resizeRowsToContents()
        finally:
            self.table.blockSignals(False)
            self._rendering = False

    def _change(self, source: str, **changes) -> None:
        if self._rendering or self._catalog is None:
            return
        record = next(record for record in self._records if record.source == source)
        if "destination" in changes:
            destination = changes["destination"]
            if not _valid_destination(destination):
                self._reject("Destino no válido: indique una ruta absoluta sin caracteres de control.")
                self._render()
                return
        if "kind" in changes:
            changes["confidence"] = "manual"
            old_default = self._roots().get(KIND_PATHS.get(record.kind, ""), "")
            if not record.destination or record.destination == old_default:
                changes["destination"] = self._proposed_destination(changes["kind"])
        self._save(replace(record, **changes))
        self._render()

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._rendering:
            return
        record = self._records[item.row()]
        column, text = item.column(), item.text().strip()
        changes = {}
        if column == 2:
            if not text:
                self._reject("El título no puede estar vacío.")
                self._render()
                return
            changes = {"title": text, "confidence": "manual"}
        elif column == 3:
            if text in ("", "—"):
                season = None
            elif text.isascii() and text.isdigit() and len(text) <= 3:
                season = int(text)
            else:
                self._reject("Temporada no válida: use un entero de 0 a 999 o deje el campo vacío.")
                self._render()
                return
            changes = {"season": season, "confidence": "manual"}
        elif column == 6:
            changes = {"included": item.checkState() == Qt.Checked and not record.error}
        if changes:
            self._change(record.source, **changes)

    def commit_edits(self) -> bool:
        focused = QApplication.focusWidget()
        if focused is not None and self.table.isAncestorOf(focused):
            self._last_save_error = None
            self.add_button.setFocus(Qt.OtherFocusReason)
            return self._last_save_error is None
        return True

    def set_remote_torrents(self, torrents: list[dict] | None) -> None:
        self.commit_edits()
        rejected = self._last_save_error
        self._remote_torrents = torrents
        failed = False
        if torrents is not None and self._catalog is not None:
            for record in list(self._records):
                exact, _similar = find_matches(record.info_hash_v1, record.info_hash_v2,
                                               record.title, torrents)
                if exact and record.included:
                    if not self._save(replace(record, included=False)):
                        failed = True
                        break
        self._render()
        if not failed and self._catalog is not None:
            self._summary()
        if rejected and not failed:
            # Un guardado automático de la consulta no debe tapar el rechazo.
            self._reject(rejected)

    def close_catalog(self) -> None:
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None
