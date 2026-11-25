"""Coincidencias de revisión: identidad exacta frente a indicios por título."""

import re
import unicodedata
from difflib import SequenceMatcher

from .classification import classify


def _normalized_title(text: str) -> str:
    # Añadir extensión evita que Path.stem recorte un sufijo del nombre remoto.
    title = classify(text + ".torrent").title
    normalized = unicodedata.normalize("NFKD", title.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(
        char for char in normalized if not unicodedata.combining(char)
    )))


def _valid_hash(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", value):
        return value.lower()
    return None


def find_matches(
    info_hash_v1: str | None,
    info_hash_v2: str | None,
    title: str,
    candidates: list[dict],
) -> tuple[list[dict], list[dict]]:
    """No transforma ni elimina candidatos; los títulos nunca prueban identidad."""
    hashes = {h for value in (info_hash_v1, info_hash_v2) if (h := _valid_hash(value))}
    own_title = _normalized_title(title)
    exact, similar = [], []
    for candidate in candidates:
        other_hashes = {
            h for key in ("hash", "info_hash_v1", "info_hash_v2", "infohash_v1", "infohash_v2")
            if (h := _valid_hash(candidate.get(key)))
        }
        if hashes & other_hashes:
            exact.append(candidate)
        else:
            other_title = _normalized_title(candidate.get("name", candidate.get("title", "")))
            if own_title and (own_title == other_title or (
                min(len(own_title), len(other_title)) >= 6
                and SequenceMatcher(None, own_title, other_title).ratio() >= 0.9
            )):
                similar.append(candidate)
    return exact, similar
