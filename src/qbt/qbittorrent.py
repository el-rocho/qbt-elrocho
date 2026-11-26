"""Cliente de solo lectura para la Web API de qBittorrent."""

import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 2_000_000
MAX_ENTRIES = 2_000
_VERSION_TEXT = re.compile(r"^v?\d+(?:\.\d+){1,3}$")


class QBittorrentError(RuntimeError):
    """Error de transporte o respuesta de la API, sin incluir secretos."""


def _is_valid_progress(value: object) -> bool:
    """El progreso debe ser un número finito dentro de [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


class QBittorrentClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 8.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        # timeout es el de urllib: inactividad por operación de socket, no un
        # plazo total para la consulta completa.
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_entries = max_entries

    def _get(self, endpoint: str, params: dict[str, str] | None = None) -> bytes:
        url = f"{self.base_url}/api/v2/{endpoint}"
        if params:
            url += "?" + urlencode(params)
        headers = {"Accept": "application/json, text/plain"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            # HTTPError también es una respuesta abierta; se cierra antes de
            # traducir el error para no retener sockets entre reintentos.
            exc.close()
            if exc.code == 403:
                raise QBittorrentError(
                    f"Acceso no autorizado (HTTP 403) para {endpoint}. "
                    "Compruebe la lista blanca o la sesión de la WebUI."
                ) from None
            if exc.code == 401:
                raise QBittorrentError(
                    f"No autenticado (HTTP 401) para {endpoint}. La WebUI exige iniciar sesión."
                ) from None
            raise QBittorrentError(f"qBittorrent devolvió HTTP {exc.code} para {endpoint}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise QBittorrentError(f"No se pudo conectar con qBittorrent ({type(exc).__name__})") from None
        if len(content) > self.max_response_bytes:
            raise QBittorrentError(
                f"La respuesta de {endpoint} supera el límite de {self.max_response_bytes} bytes"
            )
        if not content:
            raise QBittorrentError(f"Respuesta vacía de qBittorrent para {endpoint}")
        return content

    @staticmethod
    def _decode_text(content: bytes, endpoint: str) -> str:
        try:
            return content.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}") from None

    def _json_list(self, endpoint: str, params: dict[str, str]) -> list[dict]:
        content = self._get(endpoint, params)
        try:
            data = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}") from None
        if not isinstance(data, list):
            raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: se esperaba una lista")
        if len(data) > self.max_entries:
            raise QBittorrentError(
                f"La respuesta de {endpoint} supera el límite de {self.max_entries} entradas"
            )
        return data

    def version(self) -> str:
        endpoint = "app/version"
        text = self._decode_text(self._get(endpoint), endpoint)
        if not _VERSION_TEXT.match(text):
            raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}")
        return text

    def webapi_version(self) -> str:
        endpoint = "app/webapiVersion"
        text = self._decode_text(self._get(endpoint), endpoint)
        if not _VERSION_TEXT.match(text):
            raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}")
        return text

    def torrents_info(self, include_files: bool = False) -> list[dict]:
        endpoint = "torrents/info"
        params = {"includeFiles": str(include_files).lower()}
        entries = self._json_list(endpoint, params)
        for entry in entries:
            if not isinstance(entry, dict):
                raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: entrada no es objeto")
            if not isinstance(entry.get("name"), str) or not entry["name"].strip():
                raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: falta el nombre")
            if "state" in entry and not isinstance(entry["state"], str):
                raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: estado no válido")
            if "progress" in entry and not _is_valid_progress(entry["progress"]):
                raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: progreso no válido")
            if "save_path" in entry and not isinstance(entry["save_path"], str):
                raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: destino no válido")
        return entries

    def directory_content(self, path: str, with_metadata: bool = True) -> list[dict]:
        """Lista solo el nivel inmediato; la API no es recursiva."""
        endpoint = "app/getDirectoryContent"
        params = {"dirPath": path, "withMetadata": str(with_metadata).lower()}
        entries = self._json_list(endpoint, params)
        if with_metadata:
            for entry in entries:
                if not isinstance(entry, dict):
                    raise QBittorrentError(
                        f"Respuesta no válida de qBittorrent para {endpoint}: entrada no es objeto"
                    )
                if not isinstance(entry.get("name"), str) or not entry["name"].strip():
                    raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: falta el nombre")
                if not isinstance(entry.get("type"), str) or entry["type"] not in {"dir", "file"}:
                    raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: tipo no válido")
                if "size" in entry and (
                    isinstance(entry["size"], bool) or not isinstance(entry["size"], int) or entry["size"] < 0
                ):
                    raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}: tamaño no válido")
        return entries
