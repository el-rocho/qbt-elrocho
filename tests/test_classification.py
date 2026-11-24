import unittest
from pathlib import Path

from qbt.classification import classify


class ClassificationTests(unittest.TestCase):
    def test_prefixed_season_episode(self):
        result = classify(Path("[Torrentland][18695]Tierra.de.Mafiosos.S02E05.1080p.WEB-DL.torrent"))
        self.assertEqual((result.kind, result.title, result.season, result.quality), ("Serie", "Tierra de Mafiosos", 2, "1080p"))

    def test_season_word(self):
        result = classify("Example.Show.Season.02.720p.torrent")
        self.assertEqual((result.title, result.season), ("Example Show", 2))

    def test_unknown_is_not_guessed_as_movie(self):
        result = classify("unrecognizable-name.torrent")
        self.assertEqual(result.kind, "Sin determinar")
        self.assertIsNone(result.season)

    def test_books_folder(self):
        result = classify("/downloads/Libros/Autor - Título.torrent")
        self.assertEqual(result.kind, "Libro")


if __name__ == "__main__":
    unittest.main()
