import unittest
from types import SimpleNamespace

from qbt.config import DirectoryConfig
from qbt.destinations import candidate_destinations, normalize_title, propose_destination


def rec(title, destination="", kind="Serie"):
    return SimpleNamespace(title=title, destination=destination, kind=kind)


def dirs(*pairs):
    return [DirectoryConfig(path, kind) for path, kind in pairs]


class NormalizeTitleTests(unittest.TestCase):
    def test_lowercases_and_strips_tags_dots_and_underscores(self):
        self.assertEqual(normalize_title("[TAG] Mi.Serie_S01"), "mi serie s01")

    def test_collapses_whitespace_and_folds_case(self):
        self.assertEqual(normalize_title("  Tierra   de\nMafiosos "), "tierra de mafiosos")


class ProposeDestinationTests(unittest.TestCase):
    def test_reuses_prior_destination_with_same_title(self):
        prior = [rec("Tierra de Mafiosos", "/mnt/series/mafiosos")]
        result = propose_destination(
            "Serie", "Tierra.de.Mafiosos", dirs(("/mnt/series", "Serie")), prior
        )
        self.assertEqual(result, "/mnt/series/mafiosos")

    def test_reuses_existing_folder_for_series(self):
        cache = {"/mnt/series": [{"name": "Tierra de Mafiosos", "type": "dir"}]}
        result = propose_destination(
            "Serie", "Tierra de Mafiosos", dirs(("/mnt/series", "Serie")), (), cache
        )
        self.assertEqual(result, "/mnt/series")

    def test_single_concrete_directory_is_proposed(self):
        result = propose_destination("Serie", "Nueva Serie", dirs(("/mnt/series", "Serie")))
        self.assertEqual(result, "/mnt/series")

    def test_multiple_concrete_directories_require_choice(self):
        result = propose_destination(
            "Serie", "Nueva Serie",
            dirs(("/mnt/series/a", "Serie"), ("/mnt/series/b", "Serie")),
        )
        self.assertEqual(result, "")

    def test_prior_destination_counts_as_second_root(self):
        prior = [rec("Otra Serie", "/mnt/series/otra")]
        result = propose_destination("Serie", "Nueva Serie", dirs(("/mnt/series", "Serie")), prior)
        self.assertEqual(result, "")

    def test_fallback_directory_when_no_concrete(self):
        result = propose_destination("Película", "La Película", dirs(("/mnt/general", "Sin determinar")))
        self.assertEqual(result, "/mnt/general")

    def test_no_candidates_returns_empty(self):
        self.assertEqual(propose_destination("Serie", "X", dirs(("/mnt/libros", "Libro"))), "")

    def test_otros_kind_maps_to_otros_directory(self):
        result = propose_destination("Otros", "Documental", dirs(("/mnt/otros", "Otros")))
        self.assertEqual(result, "/mnt/otros")

    def test_sin_determinar_kind_maps_to_catch_all_directory(self):
        result = propose_destination(
            "Sin determinar", "Archivo suelto", dirs(("/mnt/general", "Sin determinar"))
        )
        self.assertEqual(result, "/mnt/general")


class CandidateDestinationsTests(unittest.TestCase):
    def test_deduplicates_concrete_fallback_and_prior(self):
        prior = [rec("A", "/mnt/general", kind="Película")]
        result = candidate_destinations(
            "Película",
            dirs(("/mnt/peliculas", "Película"), ("/mnt/general", "Sin determinar")),
            prior,
        )
        self.assertEqual(result, ["/mnt/peliculas", "/mnt/general"])


if __name__ == "__main__":
    unittest.main()
