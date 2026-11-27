import unittest
from types import SimpleNamespace

from qbt.config import DirectoryConfig
from qbt.destinations import candidate_destinations, normalize_title, propose_destination


def rec(title, destination="", kind="Serie", season=None, server=""):
    return SimpleNamespace(title=title, destination=destination, kind=kind, season=season,
                           server=server)


def dirs(*pairs):
    return [DirectoryConfig(path, kind) for path, kind in pairs]


def server_dirs(server, *pairs):
    return [DirectoryConfig(path, kind, server) for path, kind in pairs]


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

    def test_series_prefers_destination_of_same_season(self):
        prior = [
            rec("Tierra de Mafiosos", "/mnt/series/a/mafiosos", season=1),
            rec("Tierra de Mafiosos", "/mnt/series/b/mafiosos", season=2),
        ]
        result = propose_destination(
            "Serie", "Tierra de Mafiosos",
            dirs(("/mnt/series/a", "Serie"), ("/mnt/series/b", "Serie")),
            prior, season=2,
        )
        self.assertEqual(result, "/mnt/series/b/mafiosos")

    def test_series_without_season_match_falls_back_to_title(self):
        prior = [rec("Tierra de Mafiosos", "/mnt/series/a/mafiosos", season=1)]
        result = propose_destination(
            "Serie", "Tierra de Mafiosos", dirs(("/mnt/series/a", "Serie")), prior, season=3
        )
        self.assertEqual(result, "/mnt/series/a/mafiosos")

    def test_series_without_season_reuses_prior_destination_by_title(self):
        prior = [rec("Tierra de Mafiosos", "/mnt/series/mafiosos", season=1)]
        result = propose_destination(
            "Serie", "Tierra de Mafiosos", dirs(("/mnt/series", "Serie")), prior, season=None
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

    def test_only_directories_and_prior_of_the_requested_server_are_offered(self):
        prior = [
            rec("A", "/mnt/a", kind="Serie", server="http://servidor-a"),
            rec("B", "/mnt/b", kind="Serie", server="http://servidor-b"),
        ]
        directories = server_dirs(
            "http://servidor-a", ("/mnt/a", "Serie")
        ) + server_dirs(
            "http://servidor-b", ("/mnt/b", "Serie")
        )
        result = candidate_destinations("Serie", directories, prior, server="http://servidor-a")
        self.assertEqual(result, ["/mnt/a"])

    def test_empty_server_matches_only_empty_server(self):
        prior = [rec("A", "/mnt/a", kind="Serie", server="http://servidor-a")]
        result = candidate_destinations(
            "Serie", server_dirs("http://servidor-a", ("/mnt/a", "Serie")), prior, server=""
        )
        self.assertEqual(result, [])


class ServerIsolationTests(unittest.TestCase):
    def test_proposal_does_not_reuse_another_servers_destination(self):
        prior = [rec("Tierra de Mafiosos", "/mnt/otro-servidor/mafiosos", server="http://servidor-b")]
        result = propose_destination(
            "Serie", "Tierra de Mafiosos",
            server_dirs("http://servidor-a", ("/mnt/series", "Serie")),
            prior, server="http://servidor-a",
        )
        self.assertEqual(result, "/mnt/series")

    def test_prior_records_are_limited_to_the_requested_server(self):
        prior = [
            rec("Otra Serie", "/mnt/series/otra", server="http://servidor-a"),
            rec("Otra Serie", "/mnt/series/otra-b", server="http://servidor-b"),
        ]
        result = propose_destination(
            "Serie", "Nueva Serie",
            server_dirs("http://servidor-a", ("/mnt/series", "Serie")),
            prior, server="http://servidor-a",
        )
        # Solo cuenta el destino previo del servidor A: dos raíces → elige el usuario.
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
