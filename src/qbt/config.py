"""Configuración de dominio de la aplicación: URL de la WebUI y directorios de descarga.

Separada de las preferencias visuales (QSettings) y del catálogo de metadatos
(SQLite): aquí viven la URL base y la lista de directorios de descarga,
persistidas en un archivo JSON con escritura atómica. Las rutas son absolutas
del servidor remoto, no del disco local de la aplicación.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

SCHEMA_VERSION = 1

# Tipos de directorio. "Sin determinar" actúa como comodín (respaldo): un
# directorio donde el usuario guarda archivos de cualquier tipo.
DIRECTORY_KINDS = ("Película", "Serie", "Libro", "Otros", "Sin determinar")


class ConfigError(ValueError):
    """Configuración ilegible, incompatible o con datos inválidos."""


@dataclass(frozen=True)
class DirectoryConfig:
    path: str
    kind: str


@dataclass(frozen=True)
class AppConfig:
    base_url: str = ""
    directories: tuple[DirectoryConfig, ...] = ()


def _is_absolute_path(value: str) -> bool:
    """Ruta absoluta POSIX o Windows, sin caracteres de control."""
    if not value or any(ord(char) < 32 for char in value):
        return False
    return value.startswith("/") or PureWindowsPath(value).is_absolute()


def _validate_directory(path: object, kind: object) -> DirectoryConfig:
    if not isinstance(path, str):
        raise ConfigError("La ruta del directorio debe ser texto")
    if any(ord(char) < 32 for char in path):
        raise ConfigError("La ruta del directorio no puede contener caracteres de control")
    path = path.strip()
    if not _is_absolute_path(path):
        raise ConfigError(
            "La ruta del directorio debe ser absoluta "
            "(empezar por / o por una unidad de Windows)"
        )
    if kind not in DIRECTORY_KINDS:
        raise ConfigError(
            f"Tipo de directorio no válido: {kind!r}. "
            f"Use uno de {', '.join(DIRECTORY_KINDS)}."
        )
    return DirectoryConfig(path, kind)


def validate_directories(directories) -> tuple[DirectoryConfig, ...]:
    """Valida y normaliza una lista de directorios; lanza ConfigError si es inválida."""
    seen: set[str] = set()
    result = []
    for directory in directories:
        if not isinstance(directory, DirectoryConfig):
            raise ConfigError("Cada directorio debe ser de tipo DirectoryConfig")
        directory = _validate_directory(directory.path, directory.kind)
        if directory.path in seen:
            raise ConfigError(f"Directorio duplicado: {directory.path}")
        seen.add(directory.path)
        result.append(directory)
    return tuple(result)


def _validate(config: AppConfig) -> AppConfig:
    """Normaliza y valida una configuración completa; lanza ConfigError si no es válida."""
    if not isinstance(config, AppConfig):
        raise ConfigError("Se esperaba una configuración de tipo AppConfig")
    if not isinstance(config.base_url, str):
        raise ConfigError("La URL base debe ser texto")
    if any(ord(char) < 32 for char in config.base_url):
        raise ConfigError("La URL base no puede contener caracteres de control")
    return replace(config, directories=validate_directories(config.directories))


def load(path: str | Path) -> AppConfig:
    """Lee la configuración; si el archivo no existe devuelve la configuración vacía."""
    target = Path(str(path)).expanduser()
    if not target.exists():
        return AppConfig()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"No se pudo leer la configuración: {exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"La configuración no es JSON válido: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError("La configuración debe ser un objeto JSON")
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"Versión de configuración desconocida: {version!r} "
            f"(se esperaba {SCHEMA_VERSION}); no se modifica el archivo"
        )
    base_url = data.get("base_url", "")
    directories = data.get("directories", [])
    if not isinstance(directories, list):
        raise ConfigError("El campo 'directories' debe ser una lista")
    raw_directories = []
    for entry in directories:
        if not isinstance(entry, dict):
            raise ConfigError("Cada directorio debe ser un objeto JSON")
        raw_directories.append(DirectoryConfig(entry.get("path", ""), entry.get("kind", "")))
    return _validate(AppConfig(base_url=base_url, directories=tuple(raw_directories)))


def save(config: AppConfig, path: str | Path) -> None:
    """Guarda la configuración de forma atómica tras validarla."""
    validated = _validate(config)
    target = Path(str(path)).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"No se pudo crear la carpeta de configuración: {exc}") from None
    payload = {
        "version": SCHEMA_VERSION,
        "base_url": validated.base_url,
        "directories": [
            {"path": directory.path, "kind": directory.kind}
            for directory in validated.directories
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(target.parent), prefix=".qbt-config-", delete=False
        ) as handle:
            handle.write(text)
            temp_name = handle.name
        os.replace(temp_name, target)
    except OSError as exc:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise ConfigError(f"No se pudo guardar la configuración: {exc}") from None
