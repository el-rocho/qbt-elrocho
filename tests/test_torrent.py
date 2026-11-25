import hashlib
import unittest
from pathlib import Path
import tempfile

from qbt.torrent import TorrentError, TorrentIdentity, read_torrent


def bencode(value):
    """Codificador mínimo e independiente, sólo para construir fixtures."""
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int) and not isinstance(value, bool):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            bencode(key) + bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))


class TorrentFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, content, name="sample.torrent"):
        path = self.dir / name
        path.write_bytes(content)
        return path


V1_INFO = {
    "name": "Pelicula.2020.1080p.mkv",
    "piece length": 16384,
    "pieces": b"\x11" * 20,
    "length": 1234,
}


def v1_torrent(info=None, **outer):
    info_bytes = bencode(V1_INFO if info is None else info)
    announce = outer.pop("announce", b"http://tracker.example/announce")
    payload = dict({"announce": announce, "info": b"PLACEHOLDER"}, **outer)
    return payload, info_bytes


class ReadTorrentV1Tests(TorrentFixture):
    def test_reads_single_file_v1_identity(self):
        info_bytes = bencode(V1_INFO)
        path = self.write(b"d4:info" + info_bytes + b"e")

        identity = read_torrent(path)

        self.assertEqual(identity.info_hash_v1, hashlib.sha1(info_bytes).hexdigest())
        self.assertIsNone(identity.info_hash_v2)
        self.assertEqual(identity.content_name, "Pelicula.2020.1080p.mkv")

    def test_hash_ignores_outer_dictionary_variation(self):
        info_bytes = bencode(V1_INFO)
        plain = self.write(b"d4:info" + info_bytes + b"e", "plain.torrent")
        # Diccionario externo con claves sin ordenar y campos extra: el rango
        # de bytes de 'info' no cambia, así que el infohash debe coincidir.
        extra = self.write(
            b"d4:testi1e7:comment4:hola4:info" + info_bytes + b"e",
            "extra.torrent",
        )

        first = read_torrent(plain)
        second = read_torrent(extra)

        self.assertEqual(first.info_hash_v1, second.info_hash_v1)
        self.assertEqual(first.info_hash_v1, hashlib.sha1(info_bytes).hexdigest())


V2_INFO = {
    "file tree": {"Pelicula.2020.1080p.mkv": {"": {"length": 1234, "pieces root": b"\x22" * 32}}},
    "meta version": 2,
    "name": "Pelicula.2020.1080p.mkv",
    "piece length": 16384,
}


class ReadTorrentV2Tests(TorrentFixture):
    def test_reads_v2_only_identity(self):
        info_bytes = bencode(V2_INFO)
        path = self.write(b"d4:info" + info_bytes + b"e")

        identity = read_torrent(path)

        self.assertEqual(identity.info_hash_v2, hashlib.sha256(info_bytes).hexdigest())
        self.assertIsNone(identity.info_hash_v1)
        self.assertEqual(identity.content_name, "Pelicula.2020.1080p.mkv")

    def test_hybrid_exposes_both_identities(self):
        info = dict(V2_INFO)
        info["pieces"] = b"\x33" * 20
        info["length"] = 1234
        info_bytes = bencode(info)
        path = self.write(b"d4:info" + info_bytes + b"e")

        identity = read_torrent(path)

        self.assertEqual(identity.info_hash_v1, hashlib.sha1(info_bytes).hexdigest())
        self.assertEqual(identity.info_hash_v2, hashlib.sha256(info_bytes).hexdigest())


class ReadTorrentRejectionTests(TorrentFixture):
    def assert_rejected(self, content, fragment=None):
        path = self.write(content)
        with self.assertRaises(TorrentError) as caught:
            read_torrent(path)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_rejects_empty_info_dictionary(self):
        self.assert_rejected(b"d4:infodee", "'name'")

    def test_rejects_missing_info(self):
        self.assert_rejected(b"d8:announce0:e", "info")

    def test_rejects_non_dictionary_root(self):
        self.assert_rejected(b"li1ee", "raíz")

    def test_rejects_duplicate_keys_at_root(self):
        info_bytes = bencode(V1_INFO)
        self.assert_rejected(
            b"d4:info" + info_bytes + b"4:info" + info_bytes + b"e",
            "duplicada",
        )

    def test_rejects_duplicate_keys_inside_info(self):
        info = bencode(V1_INFO)
        broken = b"d" + info[1:-1] + b"4:name3:xde"
        self.assert_rejected(b"d4:info" + broken + b"e", "duplicada")

    def test_rejects_truncated_document(self):
        info_bytes = bencode(V1_INFO)
        self.assert_rejected(b"d4:info" + info_bytes[:-3], "truncada")

    def test_rejects_trailing_bytes(self):
        info_bytes = bencode(V1_INFO)
        self.assert_rejected(b"d4:info" + info_bytes + b"ee", "adicionales")

    def test_rejects_pieces_not_multiple_of_twenty(self):
        info = dict(V1_INFO, pieces=b"\x00" * 21)
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "múltiplo de 20")

    def test_rejects_missing_length_and_files(self):
        info = {key: value for key, value in V1_INFO.items() if key != "length"}
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "length")

    def test_rejects_length_and_files_together(self):
        info = dict(V1_INFO, files=[{"length": 1, "path": [b"a"]}])
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "a la vez")

    def test_rejects_v1_file_entry_without_path(self):
        info = {key: value for key, value in V1_INFO.items() if key != "length"}
        info["files"] = [{"length": 1}]
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "path")

    def test_rejects_hybrid_with_pieces_but_no_v1_layout(self):
        info = {key: value for key, value in V2_INFO.items()}
        info["pieces"] = b"\x66" * 20
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "length")

    def test_rejects_unsupported_meta_version(self):
        info = dict(V2_INFO, **{"meta version": 3})
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "no soportada")

    def test_rejects_v2_empty_file_tree(self):
        info = dict(V2_INFO, **{"file tree": {}})
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "vacío")

    def test_rejects_v2_file_tree_leaf_without_length(self):
        info = dict(V2_INFO, **{"file tree": {"a.mkv": {"": {"pieces root": b"\x00" * 32}}}})
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "length")

    def test_rejects_v2_piece_length_under_sixteen_kib(self):
        info = dict(V2_INFO, **{"piece length": 4096})
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "potencia de dos")

    def test_rejects_deeply_nested_bencode(self):
        self.assert_rejected(b"l" * 40 + b"e" * 40, "profundidad")

    def test_rejects_document_over_eight_mebibytes(self):
        padding = b"x" * (8 * 1024 * 1024)
        path = self.write(b"d4:junk" + str(len(padding)).encode() + b":" + padding + b"4:info" + bencode(V1_INFO) + b"e")
        with self.assertRaises(TorrentError) as caught:
            read_torrent(path)
        self.assertIn("límite", str(caught.exception))

    def test_rejects_missing_file(self):
        with self.assertRaises(TorrentError) as caught:
            read_torrent(self.dir / "no-existe.torrent")
        self.assertIn("No se pudo leer", str(caught.exception))

    def test_accepts_multi_file_v1_and_path_objects(self):
        info = {
            "name": "Serie",
            "piece length": 16384,
            "pieces": b"\x44" * 40,
            "files": [
                {"length": 10, "path": [b"Temporada 1", b"01.mkv"]},
                {"length": 0, "path": [b"leeme.txt"]},
            ],
        }
        info_bytes = bencode(info)
        identity = read_torrent(Path(self.write(b"d4:info" + info_bytes + b"e")))

        self.assertEqual(identity.info_hash_v1, hashlib.sha1(info_bytes).hexdigest())
        self.assertEqual(identity.content_name, "Serie")

    def test_rejects_too_many_elements(self):
        self.assert_rejected(b"d4:junkl" + b"i0e" * 100_001 + b"ee", "entradas")

    def test_rejects_absurdly_long_integer(self):
        self.assert_rejected(b"d4:infoi" + b"9" * 5000 + b"ee", "demasiado largo")

    def test_rejects_absurdly_long_string_length(self):
        self.assert_rejected(b"d4:info" + b"9" * 5000 + b":x", "demasiado larga")

    def test_rejects_integer_with_leading_zeros(self):
        self.assert_rejected(b"d4:infoi01ee", "ceros a la izquierda")

    def test_rejects_negative_zero_integer(self):
        self.assert_rejected(b"d4:infoi-0ee", "negativo cero")

    def test_rejects_string_length_with_leading_zero(self):
        self.assert_rejected(b"d4:info01:xe", "ceros a la izquierda")

    def test_rejects_v2_file_tree_with_empty_file_name(self):
        info = dict(V2_INFO, **{"file tree": {"": {"": {"length": 1}}}})
        self.assert_rejected(b"d4:info" + bencode(info) + b"e", "nombre de archivo vacío")

    def test_rejects_empty_content_name(self):
        self.assert_rejected(b"d4:infod4:name0:12:piece lengthi16e6:pieces20:" + b"0" * 20 + b"ee", "'name'")

    def test_decodes_non_utf8_name_without_crashing(self):
        info = {
            "name": b"Pel\xedcula.mkv",
            "piece length": 16384,
            "pieces": b"\x55" * 20,
            "length": 1,
        }
        identity = read_torrent(self.write(b"d4:info" + bencode(info) + b"e"))

        self.assertIn("cula.mkv", identity.content_name)

    def test_reads_exactly_at_eight_mebibyte_boundary(self):
        info_bytes = bencode(V1_INFO)
        limit = 8 * 1024 * 1024
        tail = b"4:info" + info_bytes + b"e"
        length = limit - len(tail) - 8
        while True:
            padded = b"d4:junk" + str(length).encode() + b":" + b"x" * length + tail
            if len(padded) == limit:
                break
            length -= 1
        self.assertEqual(len(padded), limit)

        identity = read_torrent(self.write(padded, "boundary.torrent"))

        self.assertEqual(identity.info_hash_v1, hashlib.sha1(info_bytes).hexdigest())

    def test_reads_file_with_read_only_permissions(self):
        info_bytes = bencode(V1_INFO)
        path = self.write(b"d4:info" + info_bytes + b"e")
        path.chmod(0o444)

        identity = read_torrent(path)

        self.assertEqual(identity.info_hash_v1, hashlib.sha1(info_bytes).hexdigest())
        self.assertEqual(path.read_bytes()[:2], b"d4")


class ContractShapeTests(unittest.TestCase):
    def test_torrent_identity_field_order_and_immutability(self):
        identity = TorrentIdentity("a" * 40, None, "nombre")

        self.assertEqual(
            [field for field in TorrentIdentity.__dataclass_fields__],
            ["info_hash_v1", "info_hash_v2", "content_name"],
        )
        with self.assertRaises(AttributeError):
            identity.content_name = "otro"


if __name__ == "__main__":
    unittest.main()
