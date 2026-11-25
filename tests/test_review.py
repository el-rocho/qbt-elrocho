import unittest

from qbt.review import find_matches


class ReviewTests(unittest.TestCase):
    def test_exact_identity_matches_despite_different_names(self):
        same = {"name": "Nombre remoto distinto", "hash": "a" * 40}
        other = {"name": "Ejemplo", "hash": "b" * 40}
        exact, similar = find_matches("a" * 40, None, "Ejemplo", [same, other])
        self.assertEqual(exact, [same])
        self.assertEqual(similar, [other])

    def test_similar_titles_are_warnings_not_exact_matches(self):
        remote = {"name": "[DEMO]Tierra.de.Mafiosos.S02E05.1080p.WEB-DL", "hash": "b" * 40}
        exact, similar = find_matches("a" * 40, None, "Tierra de Mafiosos", [remote])
        self.assertEqual(exact, [])
        self.assertEqual(similar, [remote])

    def test_v2_matches_full_hash_but_not_a_truncated_remote_identifier(self):
        full = {"name": "Otro nombre", "infohash_v2": "a" * 64}
        truncated = {"name": "Otro nombre", "hash": "a" * 40}
        self.assertEqual(find_matches(None, "a" * 64, "Ejemplo", [full, truncated]), ([full], []))

    def test_hashes_are_case_insensitive_and_invalid_values_never_prove_identity(self):
        candidate = {"name": "Otro nombre", "hash": "A" * 40}
        self.assertEqual(find_matches("a" * 40, None, "Ejemplo", [candidate]), ([candidate], []))
        self.assertEqual(find_matches("abc", None, "Ejemplo", [{"hash": "abc"}]), ([], []))
        self.assertEqual(find_matches("a" * 40, None, "Ejemplo", [{"hash": ["a" * 40]}]), ([], []))

    def test_unrelated_short_names_are_not_fuzzy_matches(self):
        self.assertEqual(find_matches(None, None, "It", [{"name": "Hit"}]), ([], []))


if __name__ == "__main__":
    unittest.main()
