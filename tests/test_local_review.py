import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView, QLineEdit, QMessageBox
from PySide6.QtTest import QTest

from qbt.app import MainWindow

DEFAULT_PATHS = ("/library/movies", "/library/series", "/library/books")
from qbt.catalog import CatalogError
from qbt.config import AppConfig, DirectoryConfig, save as save_config
from qbt.torrent import read_torrent


def make_torrent(path: Path, name: str = "DEMO Serie", byte: bytes = b"a") -> bytes:
    encoded = name.encode("utf-8")
    info = (b"d6:lengthi1e4:name" + str(len(encoded)).encode() + b":" + encoded
            + b"12:piece lengthi16384e6:pieces20:" + byte * 20 + b"e")
    content = b"d4:info" + info + b"e"
    path.write_bytes(content)
    return content


class LocalReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.windows = []
        save_config(AppConfig(directories=tuple(
            DirectoryConfig(path, kind) for path, kind in
            zip(DEFAULT_PATHS, ("Película", "Serie", "Libro"))
        )), self.root / "widths.json")

    def window(self):
        settings = QSettings(str(self.root / "widths.ini"), QSettings.Format.IniFormat)
        result = MainWindow(settings=settings, catalog_path=self.root / "review.sqlite3")
        self.windows.append(result)
        return result

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        QApplication.processEvents()

    def test_identical_torrents_are_excluded_but_similar_titles_are_only_warnings(self):
        first = self.root / "DEMO.Serie.S02E01.torrent"
        duplicate = self.root / "copia.torrent"
        different = self.root / "DEMO.Serie.S02E02.torrent"
        original = make_torrent(first)
        duplicate.write_bytes(original)
        make_torrent(different, byte=b"b")
        window = self.window()
        window.import_files([str(first), str(duplicate), str(different)])
        table = window.local_table
        self.assertEqual(table.item(0, 6).checkState(), Qt.Checked)
        self.assertEqual(table.item(1, 6).checkState(), Qt.Unchecked)
        self.assertIn("idéntico", table.item(1, 7).text())
        self.assertEqual(table.item(2, 6).checkState(), Qt.Checked)
        self.assertIn("parecido", table.item(2, 7).text())
        self.assertTrue(first.exists())
        self.assertTrue(duplicate.exists())

    def test_remote_exact_duplicates_exclude_rows_and_remote_data_is_discarded_on_url_change(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        identity = read_torrent(source)
        window._fill_torrents([{"name": "Servidor DEMO", "hash": identity.info_hash_v1}])
        self.assertEqual(window.local_table.item(0, 6).checkState(), Qt.Unchecked)
        self.assertIn("remoto", window.local_table.item(0, 7).text())
        window.url_edit.setText("http://127.0.0.1:12345")
        self.assertNotIn("remoto", window.local_table.item(0, 7).text())
        self.assertIn("Sin consulta remota", window.local_status_label.text())
        self.assertEqual(window.local_table.item(0, 6).checkState(), Qt.Unchecked)

    def test_multiple_series_roots_require_choice_without_appending_title_or_season(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        other = self.root / "Otra.Serie.S01E01.torrent"
        make_torrent(source)
        make_torrent(other, byte=b"b")
        window = self.window()
        window.import_files([str(source)])
        combo = window.local_table.cellWidget(0, 5)
        combo.setEditText("/archivo/series")
        combo.lineEdit().editingFinished.emit()
        window.import_files([str(other)])
        combo = window.local_table.cellWidget(1, 5)
        self.assertEqual(combo.currentText(), "")
        self.assertEqual({combo.itemText(i) for i in range(combo.count())},
                         {DEFAULT_PATHS[1], "/archivo/series"})
        self.assertIn("Elegir destino", window.local_table.item(1, 7).text())
        window.local_table.cellWidget(1, 1).setCurrentText("Película")
        self.assertEqual(window.local_table.cellWidget(1, 5).currentText(), DEFAULT_PATHS[0])

    def test_editing_season_reproposes_destination_for_series(self):
        window = self.window()
        window._app_config = AppConfig(
            base_url=window._app_config.base_url,
            directories=(DirectoryConfig("/mnt/a", "Serie"), DirectoryConfig("/mnt/b", "Serie")),
        )
        # Dos registros de la misma serie en temporadas y destinos distintos.
        first = self.root / "DEMO.Serie.S01E01.torrent"
        make_torrent(first)
        window.import_files([str(first)])
        window.local_table.cellWidget(0, 5).setEditText("/mnt/a")
        window.local_table.cellWidget(0, 5).lineEdit().editingFinished.emit()
        second = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(second, byte=b"b")
        window.import_files([str(second)])
        window.local_table.cellWidget(1, 5).setEditText("/mnt/b")
        window.local_table.cellWidget(1, 5).lineEdit().editingFinished.emit()
        # Nueva fila de la misma serie en temporada 3: sin coincidencia de
        # temporada cae al destino del título (el primero guardado).
        third = self.root / "DEMO.Serie.S03E01.torrent"
        make_torrent(third, byte=b"c")
        window.import_files([str(third)])
        self.assertEqual(window.local_table.cellWidget(2, 5).currentText(), "/mnt/a")
        # Al corregir la temporada a 2 se reutiliza el destino de esa temporada.
        window.local_table.item(2, 3).setText("2")
        self.assertEqual(window.local_table.cellWidget(2, 5).currentText(), "/mnt/b")

    def test_invalid_destination_does_not_replace_saved_value(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        for invalid in ("relativa/series", "/destino\ninyectado"):
            combo = window.local_table.cellWidget(0, 5)
            combo.setEditText(invalid)
            combo.lineEdit().editingFinished.emit()
            self.assertEqual(window.local_table.cellWidget(0, 5).currentText(), DEFAULT_PATHS[1])
            self.assertIn("ruta absoluta", window.local_status_label.text())

    def test_opening_destination_popup_does_not_rebuild_and_close_it(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        combo = window.local_table.cellWidget(0, 5)
        combo.showPopup()
        QApplication.processEvents()
        # Al abrir el popup el lineEdit pierde el foco y emite editingFinished
        # con el texto sin cambios: no debe reconstruirse el desplegable (eso
        # cerraría el popup y haría imposible elegir).
        combo.lineEdit().editingFinished.emit()
        QApplication.processEvents()
        self.assertIs(window.local_table.cellWidget(0, 5), combo)
        self.assertTrue(combo.view().isVisible())

    def test_missing_or_changed_sources_are_excluded_on_reopen_and_can_be_reimported(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.local_table.item(0, 2).setText("Mi corrección")
        window.import_files([str(source)])
        self.assertEqual(window.local_table.rowCount(), 1)
        self.assertEqual(window.local_table.item(0, 2).text(), "Mi corrección")
        window.close()
        make_torrent(source, byte=b"b")
        restored = self.window()
        self.assertIn("cambiado", restored.local_table.item(0, 7).text())
        self.assertEqual(restored.local_table.item(0, 6).checkState(), Qt.Unchecked)
        restored.import_files([str(source)])
        self.assertNotIn("cambiado", restored.local_table.item(0, 7).text())
        self.assertEqual(restored.local_table.rowCount(), 1)
        restored.close()
        source.unlink()
        missing = self.window()
        self.assertEqual(missing.local_table.item(0, 6).checkState(), Qt.Unchecked)
        self.assertIn("No se pudo leer", missing.local_table.item(0, 7).text())
        missing.local_table.item(0, 6).setCheckState(Qt.Checked)
        self.assertEqual(missing.local_table.item(0, 6).checkState(), Qt.Unchecked)

    def test_failed_save_reverts_visible_edit_without_claiming_it_was_saved(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        title = window.local_table.item(0, 2).text()
        with mock.patch.object(window.local_group._catalog, "save", side_effect=CatalogError("disco lleno")):
            window.local_table.item(0, 2).setText("Corrección que no se pudo guardar")
        self.assertEqual(window.local_table.item(0, 2).text(), title)
        self.assertIn("No se guardó", window.local_status_label.text())
        window.close()
        self.assertEqual(self.window().local_table.item(0, 2).text(), title)

    def test_invalid_torrent_is_retained_as_an_excluded_error_without_altering_source(self):
        source = self.root / "invalido.torrent"
        source.write_bytes(b"no es metainformacion")
        window = self.window()
        window.import_files([str(source)])
        self.assertEqual(window.local_table.rowCount(), 1)
        self.assertEqual(window.local_table.item(0, 6).checkState(), Qt.Unchecked)
        self.assertTrue(window.local_table.item(0, 7).text())
        self.assertEqual(source.read_bytes(), b"no es metainformacion")

    def test_unreadable_catalog_does_not_block_remote_controls_or_get_overwritten(self):
        db = self.root / "review.sqlite3"
        db.write_bytes(b"not a sqlite database")
        window = self.window()
        self.assertFalse(window.local_group.add_button.isEnabled())
        self.assertTrue(window.version_button.isEnabled())
        self.assertIn("No se pudo abrir", window.local_status_label.text())
        self.assertEqual(db.read_bytes(), b"not a sqlite database")

    def test_keyboard_edit_is_saved_after_enter(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.show()
        window.activateWindow()
        table = window.local_table
        table.setCurrentCell(0, 2)
        table.setFocus()
        QApplication.processEvents()
        QTest.keyClick(table, Qt.Key_F2)
        QApplication.processEvents()
        editor = QApplication.focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        editor.selectAll()
        QTest.keyClicks(editor, "Titulo corregido por teclado")
        QTest.keyClick(editor, Qt.Key_Return)
        QApplication.processEvents()
        self.assertEqual(table.item(0, 2).text(), "Titulo corregido por teclado")
        window.close()
        self.assertEqual(self.window().local_table.item(0, 2).text(), "Titulo corregido por teclado")

    def test_closing_window_commits_the_active_title_editor(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.show()
        window.activateWindow()
        table = window.local_table
        table.setCurrentCell(0, 2)
        table.setFocus()
        QApplication.processEvents()
        QTest.keyClick(table, Qt.Key_F2)
        QApplication.processEvents()
        editor = QApplication.focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        editor.selectAll()
        QTest.keyClicks(editor, "Pendiente al cerrar")
        window.close()
        QApplication.processEvents()
        self.assertEqual(self.window().local_table.item(0, 2).text(), "Pendiente al cerrar")

    def test_invalid_root_is_not_proposed_and_windows_destinations_remain_verbatim(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        # Sin raíz de series configurada: no se propone ningún destino.
        window._app_config = AppConfig(
            base_url=window._app_config.base_url,
            directories=(DirectoryConfig("/mnt/descargas/peliculas", "Película"),),
        )
        window.import_files([str(source)])
        self.assertEqual(window.local_table.cellWidget(0, 5).currentText(), "")
        combo = window.local_table.cellWidget(0, 5)
        combo.setEditText(r"D:\Biblioteca\Series")
        combo.lineEdit().editingFinished.emit()
        self.assertEqual(window.local_table.cellWidget(0, 5).currentText(), r"D:\Biblioteca\Series")

    def test_otros_kind_is_selectable_and_proposes_otros_directory(self):
        source = self.root / "DEMO.Documental.torrent"
        make_torrent(source, name="DEMO Documental")
        window = self.window()
        window._app_config = AppConfig(
            base_url=window._app_config.base_url,
            directories=(DirectoryConfig("/mnt/otros", "Otros"),),
        )
        window.import_files([str(source)])
        kind_combo = window.local_table.cellWidget(0, 1)
        self.assertIn("Otros", [kind_combo.itemText(i) for i in range(kind_combo.count())])
        # El torrent se clasifica como «Sin determinar»; al marcarlo «Otros» se propone su raíz.
        kind_combo.setCurrentText("Otros")
        self.assertEqual(window.local_table.cellWidget(0, 5).currentText(), "/mnt/otros")

    def test_remote_refresh_commits_an_active_local_editor(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.show()
        window.activateWindow()
        table = window.local_table
        table.setCurrentCell(0, 2)
        table.setFocus()
        QApplication.processEvents()
        QTest.keyClick(table, Qt.Key_F2)
        QApplication.processEvents()
        editor = QApplication.focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        editor.selectAll()
        QTest.keyClicks(editor, "Edicion mientras consulta")
        window._fill_torrents([])
        self.assertEqual(table.item(0, 2).text(), "Edicion mientras consulta")

    def test_changing_server_keeps_keyboard_focus_in_the_url_field(self):
        window = self.window()
        window.show()
        window.activateWindow()
        window.url_edit.setFocus()
        QApplication.processEvents()
        window.url_edit.setText("http://127.0.0.1:12345")
        self.assertIs(QApplication.focusWidget(), window.url_edit)

    def test_failed_commit_keeps_window_open_and_error_visible(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.show()
        window.activateWindow()
        table = window.local_table
        table.setCurrentCell(0, 2)
        table.setFocus()
        QApplication.processEvents()
        QTest.keyClick(table, Qt.Key_F2)
        QApplication.processEvents()
        editor = QApplication.focusWidget()
        editor.selectAll()
        QTest.keyClicks(editor, "No guardado")
        with mock.patch.object(window.local_group._catalog, "save", side_effect=CatalogError("disco lleno")):
            window.close()
        self.assertTrue(window.isVisible())
        self.assertIn("No se guardó", window.local_status_label.text())
        window._fill_torrents([])
        self.assertIn("No se guardó", window.local_status_label.text())

    def test_long_warnings_keep_compact_row_and_full_tooltip(self):
        first = self.root / "DEMO.Serie.S02E01.torrent"
        copy = self.root / "copia.torrent"
        copy.write_bytes(make_torrent(first))
        window = self.window()
        window.import_files([str(first), str(copy)])
        window.local_table.setColumnWidth(7, 150)
        window._fill_torrents([{"name": "DEMO", "hash": read_torrent(first).info_hash_v1}])
        table = window.local_table
        # El texto largo no debe inflar la fila: se recorta y el detalle completo
        # sigue disponible en el tooltip.
        self.assertLessEqual(table.rowHeight(1), table.verticalHeader().defaultSectionSize())
        self.assertEqual(table.item(1, 7).toolTip(), table.item(1, 7).text())

    def test_remove_selected_deletes_rows_without_deleting_torrent_files(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        other = self.root / "DEMO.Otra.S01E01.torrent"
        make_torrent(source)
        make_torrent(other, byte=b"b")
        window = self.window()
        window.import_files([str(source), str(other)])
        self.assertEqual(window.local_table.rowCount(), 2)

        window.local_table.selectRow(0)
        window.local_group.remove_selected()

        self.assertEqual(window.local_table.rowCount(), 1)
        self.assertEqual(window.local_table.item(0, 0).text(), Path(other).name)
        self.assertTrue(source.exists())
        self.assertTrue(other.exists())

    def test_removed_records_do_not_reappear_on_reopen(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.local_table.selectRow(0)
        window.local_group.remove_selected()
        window.close()
        window.deleteLater()
        QApplication.processEvents()

        reopened = self.window()
        self.assertEqual(reopened.local_table.rowCount(), 0)

    def test_clear_all_removes_everything_after_confirmation(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        other = self.root / "DEMO.Otra.S01E01.torrent"
        make_torrent(source)
        make_torrent(other, byte=b"b")
        window = self.window()
        window.import_files([str(source), str(other)])
        with mock.patch(
            "qbt.local_review.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            window.local_group.clear_all()

        self.assertEqual(window.local_table.rowCount(), 0)
        self.assertIn("0 revisión", window.local_status_label.text())
        self.assertTrue(source.exists())
        self.assertTrue(other.exists())

    def test_clear_all_cancelled_keeps_records(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        with mock.patch(
            "qbt.local_review.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            window.local_group.clear_all()

        self.assertEqual(window.local_table.rowCount(), 1)

    def test_combo_cells_do_not_duplicate_the_item_text(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        table = window.local_table
        # El valor lo muestra el desplegable; el item de la celda queda vacío
        # para que no se dibuje un segundo texto (fantasma) debajo.
        self.assertEqual(table.item(0, 1).text(), "")
        self.assertEqual(table.cellWidget(0, 1).currentText(), "Serie")
        self.assertEqual(table.item(0, 5).text(), "")
        self.assertEqual(table.cellWidget(0, 5).currentText(), "/library/series")

    def test_table_applies_item_selection_style(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        table = window.local_table
        self.assertIn("::item:selected", table.styleSheet())
        table.selectRow(0)
        self.assertIn(0, {index.row() for index in table.selectedIndexes()})

    def _open_cell_editor(self, window, column):
        table = window.local_table
        table.setCurrentCell(0, column)
        table.setFocus()
        QApplication.processEvents()
        QTest.keyClick(table, Qt.Key_F2)
        QApplication.processEvents()
        editor = QApplication.focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        return editor

    def _prepare_window(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        return source, window

    def test_reimporting_a_missing_source_preserves_manual_corrections(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        table = window.local_table
        table.item(0, 2).setText("Mi correccion")
        combo = table.cellWidget(0, 5)
        combo.setEditText("/elegido/a/mano")
        combo.lineEdit().editingFinished.emit()
        corrected = window.local_group._records[0]
        self.assertEqual(corrected.title, "Mi correccion")
        self.assertEqual(corrected.destination, "/elegido/a/mano")
        window.close()
        source.unlink()
        restored = self.window()
        restored.import_files([str(source)])
        table = restored.local_table
        record = restored.local_group._records[0]
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 2).text(), "Mi correccion")
        self.assertEqual(table.cellWidget(0, 5).currentText(), "/elegido/a/mano")
        self.assertEqual(table.item(0, 6).checkState(), Qt.Unchecked)
        self.assertEqual((record.info_hash_v1, record.info_hash_v2),
                         (corrected.info_hash_v1, corrected.info_hash_v2))
        self.assertIn("No se pudo leer", record.error)
        restored.close()
        again = self.window()
        self.assertEqual(again.local_table.item(0, 2).text(), "Mi correccion")
        self.assertEqual(again.local_table.cellWidget(0, 5).currentText(), "/elegido/a/mano")

    def test_reimporting_a_corrupt_source_preserves_corrections_and_last_identity(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        table = window.local_table
        table.item(0, 2).setText("Serie corregida")
        combo = table.cellWidget(0, 5)
        combo.setEditText("/otra/ruta")
        combo.lineEdit().editingFinished.emit()
        corrected = window.local_group._records[0]
        source.write_bytes(b"bytes corruptos")
        window.import_files([str(source)])
        record = window.local_group._records[0]
        self.assertEqual(window.local_table.rowCount(), 1)
        self.assertEqual(record.title, "Serie corregida")
        self.assertEqual(record.destination, "/otra/ruta")
        self.assertEqual((record.info_hash_v1, record.info_hash_v2),
                         (corrected.info_hash_v1, corrected.info_hash_v2))
        self.assertFalse(record.included)
        self.assertIn("Byte inesperado", record.error)
        self.assertIn("Byte inesperado", window.local_table.item(0, 7).text())
        window.close()
        restored = self.window()
        self.assertEqual(restored.local_table.item(0, 2).text(), "Serie corregida")
        self.assertEqual(restored.local_table.cellWidget(0, 5).currentText(), "/otra/ruta")

    def test_reimporting_a_valid_new_identity_resets_proposal_and_exclusion(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        window.local_table.item(0, 2).setText("Correccion manual")
        make_torrent(source, byte=b"b")
        window.import_files([str(source)])
        record = window.local_group._records[0]
        self.assertEqual(record.title, "DEMO Serie")
        self.assertEqual(record.destination, DEFAULT_PATHS[1])
        self.assertEqual(record.info_hash_v1, read_torrent(source).info_hash_v1)
        self.assertFalse(record.included)
        self.assertEqual(record.error, "")
        self.assertEqual(window.local_table.item(0, 2).text(), "DEMO Serie")

    def test_closing_with_empty_title_keeps_window_open_and_value_intact(self):
        source, window = self._prepare_window()
        editor = self._open_cell_editor(window, 2)
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key_Backspace)
        window.close()
        QApplication.processEvents()
        self.assertTrue(window.isVisible())
        self.assertIn("título no puede estar vacío", window.local_status_label.text())
        self.assertEqual(window.local_table.item(0, 2).text(), "DEMO Serie")
        self.assertEqual(window.local_group._records[0].title, "DEMO Serie")
        window.close()
        QApplication.processEvents()
        self.assertFalse(window.isVisible())
        self.assertEqual(self.window().local_table.item(0, 2).text(), "DEMO Serie")

    def test_closing_with_invalid_season_keeps_window_open_and_value_intact(self):
        source, window = self._prepare_window()
        editor = self._open_cell_editor(window, 3)
        editor.selectAll()
        QTest.keyClicks(editor, "12a")
        window.close()
        QApplication.processEvents()
        self.assertTrue(window.isVisible())
        self.assertIn("Temporada no válida", window.local_status_label.text())
        self.assertEqual(window.local_table.item(0, 3).text(), "02")
        self.assertEqual(window.local_group._records[0].season, 2)
        window.close()
        QApplication.processEvents()
        self.assertFalse(window.isVisible())
        self.assertEqual(self.window().local_table.item(0, 3).text(), "02")

    def test_closing_with_relative_destination_keeps_window_open_and_value_intact(self):
        source, window = self._prepare_window()
        combo = window.local_table.cellWidget(0, 5)
        line = combo.lineEdit()
        line.setFocus()
        QApplication.processEvents()
        line.selectAll()
        QTest.keyClicks(line, "relativa/series")
        self.assertEqual(combo.currentText(), "relativa/series")
        window.close()
        QApplication.processEvents()
        self.assertTrue(window.isVisible())
        self.assertIn("ruta absoluta", window.local_status_label.text())
        self.assertEqual(window.local_table.cellWidget(0, 5).currentText(), DEFAULT_PATHS[1])
        self.assertEqual(window.local_group._records[0].destination, DEFAULT_PATHS[1])
        window.close()
        QApplication.processEvents()
        self.assertFalse(window.isVisible())
        self.assertEqual(self.window().local_table.cellWidget(0, 5).currentText(), DEFAULT_PATHS[1])

    def test_remote_results_do_not_hide_a_rejected_local_edit(self):
        source, window = self._prepare_window()
        editor = self._open_cell_editor(window, 2)
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key_Backspace)
        window.close()
        QApplication.processEvents()
        self.assertTrue(window.isVisible())
        identity = read_torrent(source)
        window._fill_torrents([{"name": "Remoto DEMO", "hash": identity.info_hash_v1}])
        self.assertIn("título no puede estar vacío", window.local_status_label.text())
        self.assertEqual(window.local_table.item(0, 2).text(), "DEMO Serie")
        self.assertEqual(window.local_table.item(0, 6).checkState(), Qt.Unchecked)

    def test_import_corrections_and_destination_survive_reopening_without_remote_results(self):
        source = self.root / "DEMO.Serie.S02E01.torrent"
        original = make_torrent(source)
        window = self.window()
        window.import_files([str(source)])
        table = window.local_table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.cellWidget(0, 5).currentText(), DEFAULT_PATHS[1])
        table.item(0, 2).setText("Serie corregida")
        table.item(0, 3).setText("3")
        destination = table.cellWidget(0, 5)
        destination.setEditText("/biblioteca/conservar/serie personalizada")
        destination.lineEdit().editingFinished.emit()
        window._fill_torrents([{"name": "Dato remoto DEMO", "hash": "b" * 40}])
        window.close()
        restored = self.window()
        table = restored.local_table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 2).text(), "Serie corregida")
        self.assertEqual(table.item(0, 3).text(), "03")
        self.assertEqual(table.cellWidget(0, 5).currentText(), "/biblioteca/conservar/serie personalizada")
        self.assertEqual(restored.torrents_table.rowCount(), 0)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(restored.torrents_table.editTriggers(), QAbstractItemView.NoEditTriggers)
        for column in (0, 4, 7):
            self.assertFalse(table.item(0, column).flags() & Qt.ItemIsEditable)


if __name__ == "__main__":
    unittest.main()
