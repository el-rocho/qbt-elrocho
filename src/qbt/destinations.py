"""Resolución de destinos a partir de los directorios configurados y del catálogo."""

from __future__ import annotations

import re

_PREFIX = re.compile(r"^(?:\[[^]]+\])+\s*")
_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Compara títulos ignorando etiquetas, separadores, mayúsculas y espacios."""
    text = _PREFIX.sub("", title or "")
    text = text.replace(".", " ").replace("_", " ")
    text = _WHITESPACE.sub(" ", text).strip()
    return text.casefold()


def candidate_destinations(kind: str, directories, prior_records=()) -> list[str]:
    """Destinos candidatos: directorios del tipo, respaldo y destinos previos del tipo."""
    result = []
    for directory in directories:
        if directory.kind == kind and directory.path not in result:
            result.append(directory.path)
    for directory in directories:
        if directory.kind == "Sin determinar" and directory.path not in result:
            result.append(directory.path)
    for record in prior_records:
        if record.kind == kind and record.destination and record.destination not in result:
            result.append(record.destination)
    return result


def propose_destination(kind: str, title: str, directories, prior_records=(), dir_cache=None) -> str:
    """Propone un destino; devuelve '' cuando debe decidirlo el usuario.

    Orden: reutilizar el destino de un registro previo con el mismo título,
    después la carpeta ya existente (solo series), después el directorio del
    tipo concreto (varios → que elija el usuario) y, por último, el respaldo.
    """
    normalized = normalize_title(title)
    # 1. Reutilizar el destino de un registro previo con el mismo título.
    for record in prior_records:
        if record.destination and normalize_title(record.title) == normalized:
            return record.destination
    # 2. Serie ya existente como carpeta en los listados de directorios.
    if kind == "Serie" and dir_cache:
        for directory in directories:
            for entry in dir_cache.get(directory.path, ()):
                if entry.get("type") == "dir" and normalize_title(entry.get("name", "")) == normalized:
                    return directory.path
    # 3. Directorios del tipo concreto (contando destinos previos del tipo).
    concrete = [directory.path for directory in directories if directory.kind == kind]
    prior = [record.destination for record in prior_records if record.kind == kind and record.destination]
    candidates = list(dict.fromkeys(path for path in (*concrete, *prior) if path))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return ""
    # 4. Respaldo: directorios «Sin determinar».
    fallback = [directory.path for directory in directories if directory.kind == "Sin determinar"]
    if len(fallback) == 1:
        return fallback[0]
    return ""
