"""Identidad local de archivos .torrent, sin modificarlos ni contactar trackers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_TORRENT_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 32
MAX_ELEMENTS = 100_000
MAX_INTEGER_DIGITS = 20
MAX_LENGTH_DIGITS = 12
_DIGITS = frozenset(b"0123456789")  # bytes -> enteros ASCII


class TorrentError(ValueError):
    """Metainformación ilegible, incompleta o no soportada."""


@dataclass(frozen=True)
class TorrentIdentity:
    info_hash_v1: str | None
    info_hash_v2: str | None
    content_name: str


@dataclass(frozen=True)
class _Node:
    value: object
    start: int
    end: int


class _Parser:
    """Bdecode estricto que conserva los rangos de bytes originales."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.elements = 0

    def parse_document(self) -> dict[bytes, _Node]:
        node = self._parse_value(0)
        if self.pos != len(self.data):
            raise TorrentError("Hay datos adicionales tras el diccionario raíz")
        if not isinstance(node.value, dict):
            raise TorrentError("La raíz del archivo no es un diccionario")
        return node.value

    def _parse_value(self, depth: int) -> _Node:
        if depth > MAX_DEPTH:
            raise TorrentError(f"La metainformación supera la profundidad máxima ({MAX_DEPTH})")
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            raise TorrentError(f"La metainformación supera las {MAX_ELEMENTS} entradas")
        if self.pos >= len(self.data):
            raise TorrentError("La metainformación está truncada")
        marker = self.data[self.pos : self.pos + 1]
        if marker == b"i":
            return self._parse_integer()
        if marker == b"l":
            return self._parse_list(depth)
        if marker == b"d":
            return self._parse_dict(depth)
        if marker[0] in _DIGITS:
            return self._parse_bytes()
        raise TorrentError(f"Byte inesperado en la posición {self.pos}: {marker!r}")

    def _parse_integer(self) -> _Node:
        start = self.pos
        end = self.data.find(b"e", start + 1)
        if end == -1:
            raise TorrentError("Entero sin terminador")
        body = self.data[start + 1 : end]
        digits = body[1:] if body.startswith(b"-") else body
        if not digits or not all(byte in _DIGITS for byte in digits):
            raise TorrentError(f"Entero malformado en la posición {start}")
        if len(digits) > MAX_INTEGER_DIGITS:
            raise TorrentError(f"Entero demasiado largo en la posición {start}")
        if len(digits) > 1 and digits.startswith(b"0"):
            raise TorrentError(f"Entero con ceros a la izquierda en la posición {start}")
        if body == b"-0":
            raise TorrentError(f"Entero negativo cero en la posición {start}")
        self.pos = end + 1
        return _Node(int(body), start, self.pos)

    def _parse_bytes(self) -> _Node:
        start = self.pos
        colon = self.data.find(b":", start)
        if colon == -1:
            raise TorrentError("Cadena sin separador de longitud")
        length_text = self.data[start:colon]
        if not length_text or not all(byte in _DIGITS for byte in length_text):
            raise TorrentError(f"Longitud de cadena malformada en la posición {start}")
        if len(length_text) > MAX_LENGTH_DIGITS:
            raise TorrentError(f"Longitud de cadena demasiado larga en la posición {start}")
        if len(length_text) > 1 and length_text.startswith(b"0"):
            raise TorrentError(f"Longitud con ceros a la izquierda en la posición {start}")
        length = int(length_text)
        body_start = colon + 1
        body_end = body_start + length
        if body_end > len(self.data):
            raise TorrentError(f"Cadena truncada en la posición {start}")
        self.pos = body_end
        return _Node(self.data[body_start:body_end], start, self.pos)

    def _parse_list(self, depth: int) -> _Node:
        start = self.pos
        self.pos += 1
        items = []
        while True:
            if self.pos >= len(self.data):
                raise TorrentError("Lista sin terminador")
            if self.data[self.pos : self.pos + 1] == b"e":
                self.pos += 1
                return _Node(items, start, self.pos)
            items.append(self._parse_value(depth + 1))

    def _parse_dict(self, depth: int) -> _Node:
        start = self.pos
        self.pos += 1
        values: dict[bytes, _Node] = {}
        while True:
            if self.pos >= len(self.data):
                raise TorrentError("Diccionario sin terminador")
            if self.data[self.pos : self.pos + 1] == b"e":
                self.pos += 1
                return _Node(values, start, self.pos)
            if self.data[self.pos] not in _DIGITS:
                raise TorrentError(f"Clave de diccionario no válida en la posición {self.pos}")
            key = self._parse_bytes()
            if key.value in values:
                raise TorrentError(f"Clave duplicada: {key.value!r}")
            values[key.value] = self._parse_value(depth + 1)


def _require_dict(node: _Node, what: str) -> dict[bytes, _Node]:
    if not isinstance(node.value, dict):
        raise TorrentError(f"{what} debe ser un diccionario")
    return node.value


def _text_field(info: dict[bytes, _Node], key: bytes) -> str:
    node = info.get(key)
    if node is None or not isinstance(node.value, bytes) or not node.value:
        raise TorrentError(f"Falta el campo de texto obligatorio {key.decode()!r}")
    return node.value.decode("utf-8", "replace")


def _positive_int(info: dict[bytes, _Node], key: bytes) -> int:
    node = info.get(key)
    if node is None or not isinstance(node.value, int) or isinstance(node.value, bool):
        raise TorrentError(f"Falta el entero obligatorio {key.decode()!r}")
    if node.value <= 0:
        raise TorrentError(f"El campo {key.decode()!r} debe ser positivo")
    return node.value


def _validate_v1_files(info: dict[bytes, _Node]) -> None:
    has_length = b"length" in info
    has_files = b"files" in info
    if has_length and has_files:
        raise TorrentError("La estructura v1 no puede declarar 'length' y 'files' a la vez")
    if not has_length and not has_files:
        raise TorrentError("La estructura v1 exige 'length' o 'files'")
    if has_length:
        node = info[b"length"]
        if not isinstance(node.value, int) or isinstance(node.value, bool) or node.value < 0:
            raise TorrentError("El campo 'length' debe ser un entero no negativo")
        return
    files = info[b"files"]
    if not isinstance(files.value, list) or not files.value:
        raise TorrentError("El campo 'files' debe ser una lista no vacía")
    for entry in files.value:
        fields = _require_dict(entry, "Cada entrada de 'files'")
        length = fields.get(b"length")
        if length is None or not isinstance(length.value, int) or isinstance(length.value, bool) or length.value < 0:
            raise TorrentError("Cada archivo de 'files' exige 'length' no negativo")
        path = fields.get(b"path")
        if path is None or not isinstance(path.value, list) or not path.value:
            raise TorrentError("Cada archivo de 'files' exige 'path' no vacío")
        if not all(isinstance(part.value, bytes) and part.value for part in path.value):
            raise TorrentError("Cada componente de 'path' debe ser texto no vacío")


def _validate_v1(info: dict[bytes, _Node]) -> None:
    _positive_int(info, b"piece length")
    pieces = info.get(b"pieces")
    if pieces is None or not isinstance(pieces.value, bytes):
        raise TorrentError("Falta el campo 'pieces' de la estructura v1")
    if len(pieces.value) % 20:
        raise TorrentError("El campo 'pieces' no es múltiplo de 20 bytes")
    _validate_v1_files(info)


def _validate_file_tree(tree: dict[bytes, _Node], depth: int = 0) -> int:
    """Cuenta hojas de 'file tree' sin validar 'piece layers' criptográficamente."""
    if depth > MAX_DEPTH:
        raise TorrentError(f"'file tree' supera la profundidad máxima ({MAX_DEPTH})")
    files = 0
    for name, node in tree.items():
        if not name:
            raise TorrentError("'file tree' contiene un nombre de archivo vacío")
        fields = _require_dict(node, "Cada entrada de 'file tree'")
        if b"" in fields:
            leaf = _require_dict(fields[b""], "La entrada de archivo de 'file tree'")
            length = leaf.get(b"length")
            if length is None or not isinstance(length.value, int) or isinstance(length.value, bool) or length.value < 0:
                raise TorrentError("Cada archivo de 'file tree' exige 'length' no negativo")
            files += 1
        else:
            files += _validate_file_tree(fields, depth + 1)
    return files


def _validate_v2(info: dict[bytes, _Node]) -> None:
    piece_length = _positive_int(info, b"piece length")
    if piece_length < 16384 or piece_length & (piece_length - 1):
        raise TorrentError("'piece length' v2 debe ser una potencia de dos de al menos 16384")
    tree = info.get(b"file tree")
    if tree is None:
        raise TorrentError("Falta el campo 'file tree' de la estructura v2")
    tree_dict = _require_dict(tree, "El campo 'file tree'")
    if not tree_dict:
        raise TorrentError("El campo 'file tree' no puede estar vacío")
    if _validate_file_tree(tree_dict) == 0:
        raise TorrentError("'file tree' no declara ningún archivo")


def read_torrent(path: str | Path) -> TorrentIdentity:
    """Lee la identidad sin regenerar bencode ni tocar el archivo original."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            data = handle.read(MAX_TORRENT_BYTES + 1)
    except OSError as exc:
        raise TorrentError(f"No se pudo leer {source.name}: {exc.strerror or exc}") from None
    if len(data) > MAX_TORRENT_BYTES:
        raise TorrentError(f"El archivo supera el límite de {MAX_TORRENT_BYTES} bytes")

    root = _Parser(data).parse_document()
    info_node = root.get(b"info")
    if info_node is None:
        raise TorrentError("Falta el diccionario 'info'")
    info = _require_dict(info_node, "El campo 'info'")
    content_name = _text_field(info, b"name")

    meta_version = info.get(b"meta version")
    if meta_version is not None:
        if not isinstance(meta_version.value, int) or isinstance(meta_version.value, bool):
            raise TorrentError("'meta version' debe ser un entero")
        if meta_version.value != 2:
            raise TorrentError(f"Versión de metainformación no soportada: {meta_version.value}")

    raw_info = data[info_node.start : info_node.end]
    if meta_version is not None:
        _validate_v2(info)
        if b"pieces" in info:
            _validate_v1(info)
            v1 = hashlib.sha1(raw_info).hexdigest()
        else:
            v1 = None
        return TorrentIdentity(v1, hashlib.sha256(raw_info).hexdigest(), content_name)

    _validate_v1(info)
    return TorrentIdentity(hashlib.sha1(raw_info).hexdigest(), None, content_name)
