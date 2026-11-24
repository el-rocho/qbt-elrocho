"""Cliente de solo lectura para la Web API de qBittorrent."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class QBittorrentError(RuntimeError):
    """Error de transporte o respuesta de la API, sin incluir secretos."""


class QBittorrentClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, endpoint: str, params: dict[str, str] | None = None):
        url = f"{self.base_url}/api/v2/{endpoint}"
        if params:
            url += "?" + urlencode(params)
        headers = {"Accept": "application/json, text/plain"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read()
        except HTTPError as exc:
            raise QBittorrentError(f"qBittorrent devolvió HTTP {exc.code} para {endpoint}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise QBittorrentError(f"No se pudo conectar con qBittorrent ({type(exc).__name__})") from None
        if not content:
            return None
        if endpoint in {"app/version", "app/webapiVersion"}:
            return content.decode("utf-8").strip()
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise QBittorrentError(f"Respuesta no válida de qBittorrent para {endpoint}") from None

    def version(self) -> str:
        return self._get("app/version")

    def webapi_version(self) -> str:
        return self._get("app/webapiVersion")

    def torrents_info(self, include_files: bool = False) -> list[dict]:
        params = {"includeFiles": str(include_files).lower()}
        return self._get("torrents/info", params)

    def directory_content(self, path: str, with_metadata: bool = True) -> list[dict]:
        params = {"dirPath": path, "withMetadata": str(with_metadata).lower()}
        return self._get("app/getDirectoryContent", params)
