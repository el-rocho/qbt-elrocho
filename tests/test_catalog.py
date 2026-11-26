import os
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qbt.catalog import Catalog, CatalogError, LocalRecord


def record(source="a.torrent", **overrides):
    fields = dict(
        source=source,
        info_hash_v1="a" * 40,
        info_hash_v2=None,
        kind="Serie",
        title="Tierra de Mafiosos",
        season=2,
        quality="1080p",
        confidence="alta",
        destination="/descargas/Series/Tierra de Mafiosos",
        included=True,
    )
    fields.update(overrides)
    return LocalRecord(**fields)


class CatalogTempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)


class CatalogMemoryTests(unittest.TestCase):
    def test_new_in_memory_catalog_is_empty(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)

        self.assertEqual(catalog.list_records(), [])

    def test_save_round_trips_every_field(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)
        saved = record(
            "b.torrent",
            info_hash_v1=None,
            info_hash_v2="b" * 64,
            kind="Pelicula",
            title="Película",
            season=None,
            quality=None,
            confidence="media",
            destination="/descargas/Películas",
            included=False,
            error="revisar nombre",
        )

        catalog.save(saved)

        self.assertEqual(catalog.list_records(), [saved])

    def test_upsert_updates_by_source_and_preserves_insertion_order(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)
        catalog.save(record("a.torrent"))
        catalog.save(record("b.torrent", info_hash_v1="c" * 40))
        catalog.save(record("a.torrent", title="Actualizado", included=False))

        records = catalog.list_records()

        self.assertEqual([item.source for item in records], ["a.torrent", "b.torrent"])
        self.assertEqual((records[0].title, records[0].included), ("Actualizado", False))

    def test_same_hash_in_two_sources_is_allowed(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)
        catalog.save(record("a.torrent"))
        catalog.save(record("b.torrent"))

        self.assertEqual(len(catalog.list_records()), 2)

    def test_delete_removes_only_the_requested_source(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)
        catalog.save(record("a.torrent"))
        catalog.save(record("b.torrent", info_hash_v1="c" * 40))

        self.assertTrue(catalog.delete("a.torrent"))
        self.assertEqual([item.source for item in catalog.list_records()], ["b.torrent"])

    def test_delete_missing_source_returns_false(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)
        catalog.save(record("a.torrent"))

        self.assertFalse(catalog.delete("inexistente.torrent"))
        self.assertEqual(len(catalog.list_records()), 1)

    def test_clear_removes_all_records_and_returns_count(self):
        catalog = Catalog(":memory:")
        self.addCleanup(catalog.close)
        catalog.save(record("a.torrent"))
        catalog.save(record("b.torrent", info_hash_v1="c" * 40))

        self.assertEqual(catalog.clear(), 2)
        self.assertEqual(catalog.list_records(), [])


class CatalogFileTests(CatalogTempDir):
    def test_creates_missing_parent_directory_and_persists(self):
        path = self.dir / "datos" / "sub" / "catalogo.sqlite3"
        catalog = Catalog(path)
        catalog.save(record("a.torrent"))
        catalog.close()

        self.assertTrue(path.is_file())
        reopened = Catalog(path)
        self.addCleanup(reopened.close)
        self.assertEqual([item.source for item in reopened.list_records()], ["a.torrent"])

    def test_delete_persists_across_reopen(self):
        path = self.dir / "catalogo.sqlite3"
        catalog = Catalog(path)
        catalog.save(record("a.torrent"))
        catalog.save(record("b.torrent", info_hash_v1="c" * 40))
        catalog.delete("a.torrent")
        catalog.close()

        reopened = Catalog(path)
        self.addCleanup(reopened.close)
        self.assertEqual([item.source for item in reopened.list_records()], ["b.torrent"])

    def test_schema_version_and_columns_are_exact(self):
        path = self.dir / "catalogo.sqlite3"
        Catalog(path).close()

        with closing(sqlite3.connect(path)) as conn, conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = [row[1] for row in conn.execute("PRAGMA table_info(records)")]

        self.assertEqual(version, 1)
        self.assertEqual(
            columns,
            [
                "source",
                "info_hash_v1",
                "info_hash_v2",
                "kind",
                "title",
                "season",
                "quality",
                "confidence",
                "destination",
                "included",
                "error",
            ],
        )

    def test_rejects_unknown_schema_version_without_rewriting(self):
        path = self.dir / "ajeno.sqlite3"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE marcador (valor TEXT)")
            conn.execute("INSERT INTO marcador VALUES ('intacto')")
            conn.execute("PRAGMA user_version=7")

        with self.assertRaises(CatalogError) as caught:
            Catalog(path)

        self.assertIn("desconocida", str(caught.exception))
        self.assertIn("7", str(caught.exception))
        with closing(sqlite3.connect(path)) as conn, conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT valor FROM marcador").fetchone()[0], "intacto")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"marcador"})

    def test_corrupt_record_is_rejected_on_read_instead_of_crashing_the_interface(self):
        path = self.dir / "corrupt.sqlite3"
        catalog = Catalog(path)
        self.addCleanup(catalog.close)
        catalog.save(record())
        conn = sqlite3.connect(path)
        try:
            conn.execute("UPDATE records SET season = 'texto'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(CatalogError):
            catalog.list_records()

    def test_memory_catalog_does_not_create_files(self):
        workdir = self.dir / "cwd"
        workdir.mkdir()
        previous = Path.cwd()
        os.chdir(workdir)
        try:
            catalog = Catalog(":memory:")
            catalog.save(record("a.torrent"))
            catalog.close()
            self.assertEqual(list(workdir.iterdir()), [])
        finally:
            os.chdir(previous)

    def test_shared_memory_uri_is_recognised_as_memory(self):
        workdir = self.dir / "cwd-uri"
        workdir.mkdir()
        previous = Path.cwd()
        os.chdir(workdir)
        try:
            catalog = Catalog("file:qbt-test?mode=memory&cache=shared")
            catalog.save(record("a.torrent"))
            self.assertEqual(len(catalog.list_records()), 1)
            catalog.close()
            self.assertEqual(list(workdir.iterdir()), [])
        finally:
            os.chdir(previous)

    def test_close_is_idempotent_and_operations_after_close_fail_clearly(self):
        catalog = Catalog(":memory:")
        catalog.close()
        catalog.close()

        with self.assertRaises(CatalogError) as caught:
            catalog.list_records()
        self.assertIn("cerrado", str(caught.exception))
        with self.assertRaises(CatalogError):
            catalog.save(record("a.torrent"))

    def test_expands_tilde_with_home_environment(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            catalog = Catalog("~/catalogo.sqlite3")
            catalog.save(record("a.torrent"))
            catalog.close()

        self.assertTrue((self.dir / "catalogo.sqlite3").is_file())

    def test_directory_path_raises_catalog_error(self):
        with self.assertRaises(CatalogError) as caught:
            Catalog(self.dir)
        self.assertIn("No se pudo abrir", str(caught.exception))

    def test_failed_write_keeps_previous_records(self):
        path = Path(self.dir / "catalogo.sqlite3")
        first = Catalog(path)
        first.save(record("a.torrent"))
        first.close()
        path.chmod(0o444)
        self.addCleanup(path.chmod, 0o644)

        readonly = Catalog(path)
        self.addCleanup(readonly.close)
        with self.assertRaises(CatalogError) as caught:
            readonly.save(record("b.torrent"))
        self.assertIn("No se pudo guardar", str(caught.exception))
        self.assertEqual([item.source for item in readonly.list_records()], ["a.torrent"])

        path.chmod(0o644)
        readonly.close()
        reopened = Catalog(path)
        self.addCleanup(reopened.close)
        self.assertEqual([item.source for item in reopened.list_records()], ["a.torrent"])


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)

    def assert_rejected(self, bad_record, fragment):
        with self.assertRaises(CatalogError) as caught:
            self.catalog.save(bad_record)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.catalog.list_records(), [])

    def test_rejects_empty_source(self):
        self.assert_rejected(record(""), "source")

    def test_rejects_non_text_title(self):
        self.assert_rejected(record(title=None), "title")

    def test_rejects_non_bool_included(self):
        self.assert_rejected(record(included=1), "included")

    def test_rejects_bool_season(self):
        self.assert_rejected(record(season=True), "season")

    def test_rejects_negative_season(self):
        self.assert_rejected(record(season=-1), "season")

    def test_accepts_none_season_and_quality(self):
        saved = record(season=None, quality=None)
        self.catalog.save(saved)
        self.assertEqual(self.catalog.list_records(), [saved])

    def test_rejects_uppercase_hash(self):
        self.assert_rejected(record(info_hash_v1="A" * 40), "info_hash_v1")

    def test_rejects_hash_with_wrong_length(self):
        self.assert_rejected(record(info_hash_v1="a" * 64), "info_hash_v1")
        self.assert_rejected(record(info_hash_v2="b" * 40), "info_hash_v2")

    def test_rejects_non_hex_hash(self):
        self.assert_rejected(record(info_hash_v2="z" * 64), "info_hash_v2")

    def test_rejects_non_text_error(self):
        self.assert_rejected(record(error=None), "error")

    def test_previous_records_survive_validation_failure(self):
        good = record("bueno.torrent")
        self.catalog.save(good)
        with self.assertRaises(CatalogError):
            self.catalog.save(record(season=-3))

        self.assertEqual(self.catalog.list_records(), [good])

    def test_source_with_sql_metacharacters_is_stored_verbatim(self):
        hostile = "x'); DROP TABLE records;--"
        saved = record(hostile)
        self.catalog.save(saved)

        self.assertEqual(self.catalog.list_records(), [saved])

    def test_list_records_returns_an_independent_list(self):
        self.catalog.save(record("a.torrent"))
        snapshot = self.catalog.list_records()
        snapshot.clear()

        self.assertEqual(len(self.catalog.list_records()), 1)


class ContractShapeTests(unittest.TestCase):
    def test_local_record_field_order_defaults_and_immutability(self):
        self.assertEqual(
            list(LocalRecord.__dataclass_fields__),
            [
                "source",
                "info_hash_v1",
                "info_hash_v2",
                "kind",
                "title",
                "season",
                "quality",
                "confidence",
                "destination",
                "included",
                "error",
            ],
        )
        self.assertEqual(LocalRecord.__dataclass_fields__["error"].default, "")
        with self.assertRaises(AttributeError):
            record("a.torrent").title = "otro"


if __name__ == "__main__":
    unittest.main()
