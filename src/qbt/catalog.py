"""Catálogo local SQLite de metadatos de torrents; sin contenido ni credenciales."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE records (
    source        TEXT PRIMARY KEY,
    info_hash_v1  TEXT,
    info_hash_v2  TEXT,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    season        INTEGER,
    quality       TEXT,
    confidence    TEXT NOT NULL,
    destination   TEXT NOT NULL,
    included      INTEGER NOT NULL CHECK (included IN (0, 1)),
    error         TEXT NOT NULL DEFAULT '',
    server        TEXT NOT NULL DEFAULT ''
)
"""

_COLUMN_LIST = (
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
    "server",
)
_COLUMNS = ", ".join(_COLUMN_LIST)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMN_LIST)
_UPDATE_ASSIGNMENTS = ", ".join(
    f"{column} = excluded.{column}" for column in _COLUMN_LIST if column != "source"
)


class CatalogError(ValueError):
    """Catálogo ilegible, incompatible o con datos inválidos."""


@dataclass(frozen=True)
class LocalRecord:
    source: str
    info_hash_v1: str | None
    info_hash_v2: str | None
    kind: str
    title: str
    season: int | None
    quality: str | None
    confidence: str
    destination: str
    included: bool
    error: str = ""
    server: str = ""


def _is_memory(path: str) -> bool:
    return path == ":memory:" or path.startswith("file:")


_HASH_LENGTHS = {"info_hash_v1": 40, "info_hash_v2": 64}
_HEX = frozenset("0123456789abcdef")


def _validate_hash(name: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise CatalogError(f"{name} debe ser texto o None")
    expected = _HASH_LENGTHS[name]
    if len(value) != expected or any(char not in _HEX for char in value):
        raise CatalogError(
            f"{name} debe ser hexadecimal en minúsculas de {expected} caracteres"
        )


def _validate_record(record: LocalRecord) -> None:
    if not isinstance(record, LocalRecord):
        raise CatalogError("save() espera una instancia de LocalRecord")
    for name in ("source", "kind", "title", "confidence", "destination", "error", "server"):
        if not isinstance(getattr(record, name), str):
            raise CatalogError(f"El campo {name} debe ser texto")
    if not record.source:
        raise CatalogError("El campo source no puede estar vacío")
    if not isinstance(record.included, bool):
        raise CatalogError("El campo included debe ser booleano")
    if record.season is not None:
        if isinstance(record.season, bool) or not isinstance(record.season, int):
            raise CatalogError("El campo season debe ser un entero o None")
        if record.season < 0:
            raise CatalogError("El campo season no puede ser negativo")
    if record.quality is not None and not isinstance(record.quality, str):
        raise CatalogError("El campo quality debe ser texto o None")
    for name in _HASH_LENGTHS:
        _validate_hash(name, getattr(record, name))


class Catalog:
    """Catálogo de sólo metadatos; la deduplicación visual corresponde a la UI."""

    def __init__(self, path: str | Path) -> None:
        raw = str(path)
        self._closed = False
        self._conn: sqlite3.Connection | None = None
        if _is_memory(raw):
            self.path = raw
        else:
            self.path = str(Path(raw).expanduser())
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CatalogError(
                    f"No se pudo crear la carpeta del catálogo: {exc}"
                ) from None
        try:
            self._conn = sqlite3.connect(self.path, uri=self.path.startswith("file:"))
        except sqlite3.Error as exc:
            self._closed = True
            raise CatalogError(f"No se pudo abrir el catálogo: {exc}") from None
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _ensure_open(self) -> None:
        if self._closed or self._conn is None:
            raise CatalogError("El catálogo está cerrado")

    def _discard(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._closed = True

    def _initialize(self) -> None:
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version not in (0, 1):
                raise CatalogError(
                    f"Versión de esquema desconocida: {version} "
                    f"(se esperaba {SCHEMA_VERSION}); no se modifica el catálogo"
                )
            with self._conn:
                if version == 0:
                    self._conn.execute(_SCHEMA)
                else:
                    # v1 → v2: se añade la asociación al servidor; los destinos
                    # anteriores quedan «sin servidor» (cadena vacía) y no se
                    # ofrecen automáticamente hasta volver a elegirlos.
                    self._conn.execute(
                        "ALTER TABLE records ADD COLUMN server TEXT NOT NULL DEFAULT ''"
                    )
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except CatalogError:
            self._discard()
            raise
        except sqlite3.Error as exc:
            self._discard()
            raise CatalogError(f"No se pudo inicializar el catálogo: {exc}") from None

    def list_records(self) -> list[LocalRecord]:
        self._ensure_open()
        try:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM records ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CatalogError(f"No se pudo leer el catálogo: {exc}") from None
        records = [
            LocalRecord(
                source=row["source"],
                info_hash_v1=row["info_hash_v1"],
                info_hash_v2=row["info_hash_v2"],
                kind=row["kind"],
                title=row["title"],
                season=row["season"],
                quality=row["quality"],
                confidence=row["confidence"],
                destination=row["destination"],
                included=bool(row["included"]),
                error=row["error"],
                server=row["server"],
            )
            for row in rows
        ]
        for row, record in zip(rows, records):
            if type(row["included"]) is not int or row["included"] not in (0, 1):
                raise CatalogError("El campo included del catálogo está dañado")
            _validate_record(record)
        return records

    def save(self, record: LocalRecord) -> None:
        self._ensure_open()
        _validate_record(record)
        values = (
            record.source,
            record.info_hash_v1,
            record.info_hash_v2,
            record.kind,
            record.title,
            record.season,
            record.quality,
            record.confidence,
            record.destination,
            int(record.included),
            record.error,
            record.server,
        )
        try:
            with self._conn:
                self._conn.execute(
                    f"INSERT INTO records ({_COLUMNS}) VALUES ({_PLACEHOLDERS}) "
                    f"ON CONFLICT (source) DO UPDATE SET {_UPDATE_ASSIGNMENTS}",
                    values,
                )
        except sqlite3.Error as exc:
            raise CatalogError(f"No se pudo guardar {record.source}: {exc}") from None

    def delete(self, source: str) -> bool:
        """Elimina un registro por su ruta de origen; devuelve si había uno."""
        self._ensure_open()
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM records WHERE source = ?", (source,)
                )
        except sqlite3.Error as exc:
            raise CatalogError(f"No se pudo eliminar {source}: {exc}") from None
        return cursor.rowcount > 0

    def clear(self) -> int:
        """Elimina todos los registros y devuelve cuántos había."""
        self._ensure_open()
        try:
            with self._conn:
                cursor = self._conn.execute("DELETE FROM records")
        except sqlite3.Error as exc:
            raise CatalogError(f"No se pudo limpiar el catálogo: {exc}") from None
        return cursor.rowcount

    def close(self) -> None:
        self._discard()
