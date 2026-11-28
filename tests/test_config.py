import json
import tempfile
import unittest
from pathlib import Path

from qbt.config import (
    AppConfig,
    ConfigError,
    DIRECTORY_KINDS,
    DirectoryConfig,
    load,
    save,
)


def directory(path="/mnt/descargas/peliculas", kind="Película"):
    return DirectoryConfig(path, kind)


def config(base_url="http://10.0.4.56:8080", directories=()):
    if isinstance(directories, DirectoryConfig):
        directories = (directories,)
    return AppConfig(base_url=base_url, directories=tuple(directories))


class ConfigTempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)


class ConfigFileTests(ConfigTempDir):
    def test_save_round_trips_every_field(self):
        path = self.dir / "qbt-elrocho.json"
        saved = config(
            "http://10.0.4.56:8080",
            directories=(
                directory("/mnt/descargas/peliculas", "Película"),
                directory("/mnt/descargas/series", "Serie"),
                directory(r"C:\descargas\otros", "Otros"),
            ),
        )

        save(saved, path)
        reopened = load(path)

        self.assertEqual(reopened, saved)

    def test_legacy_config_preserves_values_and_enables_existing_startup(self):
        path = self.dir / "legacy.json"
        original = json.dumps({"version": 1, "base_url": "http://existing:9090",
                               "directories": [{"path": "/library", "kind": "Libro"}]})
        path.write_text(original, encoding="utf-8")
        loaded = load(path)
        self.assertTrue(getattr(loaded, "setup_completed", False))
        self.assertTrue(loaded.query_on_startup)
        self.assertEqual(loaded.connection_mode, "remote")
        self.assertEqual(loaded.base_url, "http://existing:9090")
        self.assertEqual(
            loaded.directories, (DirectoryConfig("/library", "Libro", "http://existing:9090"),)
        )
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        save(loaded, path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 4)
        self.assertEqual(load(path), loaded)

    def test_v2_config_associates_existing_directories_with_base_url(self):
        path = self.dir / "v2.json"
        path.write_text(
            json.dumps({
                "version": 2,
                "base_url": "http://servidor:8080",
                "connection_mode": "remote",
                "setup_completed": True,
                "query_on_startup": True,
                "directories": [{"path": "/library", "kind": "Libro"}],
            }),
            encoding="utf-8",
        )
        loaded = load(path)
        self.assertEqual(
            loaded.directories,
            (DirectoryConfig("/library", "Libro", "http://servidor:8080"),),
        )

    def test_v3_config_keeps_per_directory_server_and_empty_mode_urls(self):
        path = self.dir / "v3.json"
        path.write_text(
            json.dumps({
                "version": 3,
                "base_url": "http://servidor:8080",
                "connection_mode": "remote",
                "setup_completed": True,
                "query_on_startup": True,
                "directories": [{"path": "/library", "kind": "Libro",
                                 "server": "http://otro:8080"}],
            }),
            encoding="utf-8",
        )
        loaded = load(path)
        self.assertEqual(
            loaded.directories,
            (DirectoryConfig("/library", "Libro", "http://otro:8080"),),
        )
        self.assertEqual(loaded.local_url, "")
        self.assertEqual(loaded.remote_url, "")

    def test_save_round_trips_remembered_mode_urls(self):
        path = self.dir / "urls.json"
        saved = AppConfig(
            base_url="http://10.0.4.56:8080",
            connection_mode="remote",
            local_url="http://127.0.0.1:9090",
            remote_url="http://192.168.0.50:8080",
        )
        save(saved, path)
        self.assertEqual(load(path), saved)

    def test_missing_file_loads_default(self):
        self.assertEqual(load(self.dir / "inexistente.json"), AppConfig())

    def test_creates_missing_parent_directory(self):
        path = self.dir / "datos" / "sub" / "config.json"
        save(config(), path)

        self.assertTrue(path.is_file())
        self.assertEqual(load(path), config())

    def test_legacy_config_without_url_does_not_complete_setup(self):
        path = self.dir / "legacy-sin-url.json"
        path.write_text(json.dumps({"version": 1, "base_url": "", "directories": []}), encoding="utf-8")
        loaded = load(path)
        self.assertFalse(loaded.setup_completed)
        self.assertEqual(loaded.base_url, "")
        self.assertEqual(loaded.directories, ())
        self.assertEqual(loaded.connection_mode, "remote")

    def test_rejects_invalid_json(self):
        path = self.dir / "corrupto.json"
        path.write_text("{esto no es json", encoding="utf-8")

        with self.assertRaises(ConfigError) as caught:
            load(path)
        self.assertIn("JSON", str(caught.exception))

    def test_invalid_encoding_is_a_config_error_without_rewriting(self):
        path = self.dir / "encoding.json"
        path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(ConfigError):
            load(path)
        self.assertEqual(path.read_bytes(), b"\xff\xfe\x00")

    def test_rejects_invalid_connection_options(self):
        for field, value in (("connection_mode", "unknown"),
                             ("connection_mode", []),
                             ("setup_completed", "false"),
                             ("query_on_startup", 1)):
            with self.subTest(field=field, value=value):
                path = self.dir / "invalid.json"
                data = {"version": 2, "base_url": "http://localhost:8080", field: value}
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load(path)

    def test_rejects_non_object_json(self):
        path = self.dir / "lista.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")

        with self.assertRaises(ConfigError) as caught:
            load(path)
        self.assertIn("objeto", str(caught.exception))

    def test_rejects_unknown_schema_version_without_rewriting(self):
        path = self.dir / "ajeno.json"
        path.write_text(
            json.dumps({"version": 7, "base_url": "http://x", "directories": []}),
            encoding="utf-8",
        )

        with self.assertRaises(ConfigError) as caught:
            load(path)
        self.assertIn("desconocida", str(caught.exception))
        self.assertIn("7", str(caught.exception))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 7)

    def test_corrupt_field_is_rejected_on_read(self):
        path = self.dir / "campo.json"
        path.write_text(
            json.dumps({"version": 1, "base_url": 123, "directories": []}),
            encoding="utf-8",
        )

        with self.assertRaises(ConfigError) as caught:
            load(path)
        self.assertIn("URL base", str(caught.exception))

    def test_failed_write_keeps_previous_file(self):
        readonly = self.dir / "bloqueado"
        readonly.mkdir()
        readonly_path = readonly / "config.json"
        save(config("http://vieja"), readonly_path)
        # El reemplazo atómico necesita permiso de escritura en el directorio,
        # no en el archivo: se bloquea la carpeta, no el fichero.
        readonly.chmod(0o555)
        self.addCleanup(readonly.chmod, 0o755)

        with self.assertRaises(ConfigError):
            save(config("http://nueva"), readonly_path)

        self.assertEqual(
            json.loads(readonly_path.read_text(encoding="utf-8"))["base_url"], "http://vieja"
        )

    def test_load_strips_whitespace_from_paths(self):
        path = self.dir / "espacios.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "base_url": "http://x",
                    "directories": [{"path": "  /mnt/descargas  ", "kind": "Película"}],
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            load(path).directories,
            (DirectoryConfig("/mnt/descargas", "Película", "http://x"),),
        )


class ConfigValidationTests(unittest.TestCase):
    def assert_rejected(self, bad_config, fragment):
        with self.assertRaises(ConfigError) as caught:
            save(bad_config, Path(tempfile.mkdtemp()) / "config.json")
        self.assertIn(fragment, str(caught.exception))

    def test_rejects_relative_path(self):
        self.assert_rejected(config(directories=directory("descargas")), "absoluta")

    def test_rejects_empty_path(self):
        self.assert_rejected(config(directories=directory("")), "absoluta")

    def test_rejects_control_char_in_path(self):
        self.assert_rejected(config(directories=directory("/mnt/descargas\n")), "control")

    def test_rejects_unknown_kind(self):
        self.assert_rejected(config(directories=directory(kind="Música")), "Tipo de directorio")

    def test_rejects_non_text_path(self):
        self.assert_rejected(
            AppConfig(base_url="http://x", directories=(DirectoryConfig(123, "Película"),)),
            "ruta",
        )

    def test_rejects_duplicate_path(self):
        self.assert_rejected(
            config(
                directories=(
                    directory("/mnt/descargas/peliculas", "Película"),
                    directory("/mnt/descargas/peliculas", "Serie"),
                ),
            ),
            "duplicado",
        )

    def test_rejects_non_text_base_url(self):
        with self.assertRaises(ConfigError) as caught:
            save(AppConfig(base_url=123), Path(tempfile.mkdtemp()) / "config.json")
        self.assertIn("URL base", str(caught.exception))

    def test_rejects_control_char_in_base_url(self):
        self.assert_rejected(AppConfig(base_url="http://x\n"), "control")

    def test_accepts_empty_base_url_and_no_directories(self):
        self.assertEqual(AppConfig(), AppConfig("", ()))

    def test_accepts_posix_and_windows_absolute_paths(self):
        valid = config(
            directories=(
                directory("/mnt/descargas/peliculas", "Película"),
                directory(r"C:\descargas\libros", "Libro"),
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save(valid, path)
            self.assertEqual(load(path), valid)

    def test_all_kinds_are_the_canonical_set(self):
        self.assertEqual(
            DIRECTORY_KINDS, ("Película", "Serie", "Libro", "Otros", "Sin determinar")
        )


class ContractShapeTests(unittest.TestCase):
    def test_app_config_field_order_defaults_and_immutability(self):
        self.assertEqual(
            list(AppConfig.__dataclass_fields__),
            ["base_url", "directories", "connection_mode", "setup_completed",
             "query_on_startup", "local_url", "remote_url"],
        )
        self.assertEqual(AppConfig.__dataclass_fields__["base_url"].default, "")
        self.assertEqual(AppConfig.__dataclass_fields__["directories"].default, ())
        self.assertEqual(AppConfig.__dataclass_fields__["local_url"].default, "")
        self.assertEqual(AppConfig.__dataclass_fields__["remote_url"].default, "")
        with self.assertRaises(AttributeError):
            config().base_url = "otra"

    def test_directory_config_field_order_and_immutability(self):
        self.assertEqual(list(DirectoryConfig.__dataclass_fields__), ["path", "kind", "server"])
        self.assertEqual(DirectoryConfig.__dataclass_fields__["server"].default, "")
        with self.assertRaises(AttributeError):
            directory().path = "otra"


if __name__ == "__main__":
    unittest.main()
