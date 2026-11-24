"""Clasificación local, conservadora, de nombres de archivos torrent."""

from dataclasses import dataclass
from pathlib import Path
import re

_PREFIX = re.compile(r"^(?:\[[^]]+\])+\s*")
_SEASON_EPISODE = re.compile(r"(?i)(?:\bS(\d{1,2})(?:E\d{1,3})?\b|\bSeason[ ._-]*(\d{1,2})\b)")
_QUALITY = re.compile(r"(?i)\b(480|720|1080|2160)p\b")


@dataclass(frozen=True)
class Proposal:
    source: Path
    kind: str
    title: str
    season: int | None
    quality: str | None
    confidence: str


def classify(path: str | Path) -> Proposal:
    """Interpreta el nombre sin enviar ni alterar el archivo original."""
    source = Path(path)
    stem = _PREFIX.sub("", source.stem).replace(".", " ").replace("_", " ")
    season_match = _SEASON_EPISODE.search(stem)
    quality_match = _QUALITY.search(stem)
    season = int(season_match.group(1) or season_match.group(2)) if season_match else None
    cut = min((m.start() for m in (season_match, quality_match) if m), default=len(stem))
    title = re.sub(r"\s+", " ", stem[:cut]).strip(" -._")
    if season_match:
        kind, confidence = "Serie", "alta" if title else "baja"
    elif source.parent.name.lower() in {"libros", "books"}:
        kind, confidence = "Libro", "media"
    else:
        kind, confidence = "Sin determinar", "baja"
    if not title:
        title = source.stem
    return Proposal(source, kind, title, season, quality_match.group(1) + "p" if quality_match else None, confidence)
