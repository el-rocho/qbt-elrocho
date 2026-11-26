"""Cliente de solo lectura para la Web API de qBittorrent."""

import json
import math
import re
from dataclasses import dataclass
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 2_000_000
MAX_ENTRIES = 2_000
_VERSION_TEXT = re.compile(r"^v?\d+(?:\.\d+){1,3}$")
# Primer WebAPI con `app/getDirectoryContent?withMetadata` (changelog oficial 2.11.8).
_MIN_WEBAPI_DIRECTORY_METADATA = (2, 11, 8)


class QBittorrentError(RuntimeError):
    """Error de transporte o respuesta de la API, sin incluir secretos."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        # Código HTTP cuando el fallo provino de una respuesta; None si no hubo
        # respuesta utilizable (conexión, timeout, cuerpo inválido, etc.).
        self.status_code = status_code


class QBittorrentAuthError(QBittorrentError):
    """Faltan credenciales, la sesión caducó o fue rechazada."""


@dataclass(frozen=True)
class ServerCapabilities:
    """Capacidades derivadas de la versión de WebAPI, no de permisos probados.

    ``torrents`` indica que la API v2 de torrents existe en ese servidor y
    ``directories`` que ``getDirectoryContent`` acepta ``withMetadata``. Ninguno
    de los dos afirma que el usuario autenticado tenga permiso para usarlos:
    eso solo se comprueba al llamar al endpoint correspondiente.
    """

    version: str
    webapi_version: str
    torrents: bool
    directories: bool


class _RejectRedirects(HTTPRedirectHandler):
    """Ninguna redirección se sigue: evita reenviar credenciales a otro destino."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_RejectRedirects())


def urlopen(request: Request, timeout: float | None = None):
    """Abre la petición con el opener interno que rechaza redirecciones."""
    if timeout is None:
        return _opener.open(request)
    return _opener.open(request, timeout=timeout)


def _is_valid_progress(value: object) -> bool:
    """El progreso debe ser un número finito dentro de [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _version_components(text: str, count: int = 3) -> tuple[int, ...]:
    parts = [int(part) for part in text.lstrip("vV").split(".")]
    parts = (parts + [0] * count)[:count]
    return tuple(parts)


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
        # Sesión exclusivamente en memoria: cada instancia tiene su propio jar.
        self._cookies = CookieJar()
        self._authenticated = False

    @property
    def authenticated(self) -> bool:
        """True solo tras un login aceptado con cookie de sesión en memoria."""
        return self._authenticated

    def clear_session(self) -> None:
        """Olvida la sesión local. No realiza ninguna petición de red."""
        self._cookies.clear()
        self._authenticated = False

    def _origin_headers(self) -> dict[str, str]:
        # qBittorrent exige Referer u Origin con el mismo host y puerto que el
        # Host de la petición; Origin nunca lleva ruta.
        parts = urlsplit(self.base_url)
        return {
            "Referer": f"{self.base_url}/",
            "Origin": f"{parts.scheme}://{parts.netloc}",
        }

    def _raise_http_error(self, code: int, endpoint: str) -> None:
        if code in (401, 403):
            # Un rechazo invalida la cookie: no se reintenta con ella.
            self.clear_session()
            if code == 403:
                message = (
                    f"Acceso no autorizado (HTTP 403) para {endpoint}. "
                    "Compruebe la lista blanca o la sesión de la WebUI."
                )
            else:
                message = (
                    f"No autenticado (HTTP 401) para {endpoint}. "
                    "La WebUI exige iniciar sesión."
                )
            raise QBittorrentAuthError(message, status_code=code) from None
        if 300 <= code < 400:
            raise QBittorrentError(
                f"qBittorrent devolvió una redirección (HTTP {code}) para {endpoint}; "
                "no se sigue para no exponer credenciales",
                status_code=code,
            ) from None
        raise QBittorrentError(
            f"qBittorrent devolvió HTTP {code} para {endpoint}", status_code=code
        ) from None

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, str] | None = None,
        data: bytes | None = None,
        allow_empty: bool = False,
        send_api_key: bool = True,
    ) -> bytes:
        url = f"{self.base_url}/api/v2/{endpoint}"
        if params:
            url += "?" + urlencode(params)
        headers = {"Accept": "application/json, text/plain"}
        headers.update(self._origin_headers())
        if send_api_key and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method=method)
        # CookieJar usa el nombre y valor devueltos por el servidor; no se
        # asume SID ni ningún prefijo concreto.
        self._cookies.add_cookie_header(request)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                self._cookies.extract_cookies(response, request)
                content = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            # HTTPError también es una respuesta abierta; se cierra antes de
            # traducir el error para no retener sockets entre reintentos.
            code = exc.code
            exc.close()
            self._raise_http_error(code, endpoint)
        except (URLError, TimeoutError, OSError) as exc:
            raise QBittorrentError(f"No se pudo conectar con qBittorrent ({type(exc).__name__})") from None
        if len(content) > self.max_response_bytes:
            raise QBittorrentError(
                f"La respuesta de {endpoint} supera el límite de {self.max_response_bytes} bytes"
            )
        if not content and not allow_empty:
            raise QBittorrentError(f"Respuesta vacía de qBittorrent para {endpoint}")
        return content

    def _get(self, endpoint: str, params: dict[str, str] | None = None) -> bytes:
        return self._request("GET", endpoint, params=params)

    def login(self, username: str, password: str) -> None:
        """Crea la sesión en memoria. Solo ``Ok.`` con cookie se acepta.

        Las credenciales viajan en el cuerpo del formulario y no se guardan.
        Cualquier respuesta distinta de ``Ok.``, un rechazo 401/403 o una
        redirección limpian la sesión y no dejan cookie reutilizable.
        """
        endpoint = "auth/login"
        # Se descarta cualquier sesión previa: el login sustituye la sesión.
        self.clear_session()
        data = urlencode({"username": username, "password": password}).encode("utf-8")
        try:
            content = self._request(
                "POST", endpoint, data=data, allow_empty=True, send_api_key=False
            )
        except QBittorrentError:
            self.clear_session()
            raise
        try:
            text = content.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = ""
        if text != "Ok.":
            self.clear_session()
            raise QBittorrentAuthError(
                f"qBittorrent rechazó las credenciales en {endpoint}"
            ) from None
        probe = Request(f"{self.base_url}/api/v2/app/version")
        self._cookies.add_cookie_header(probe)
        if not probe.has_header("Cookie"):
            self.clear_session()
            raise QBittorrentAuthError(
                f"qBittorrent aceptó {endpoint} pero no entregó una cookie utilizable para la API"
            ) from None
        self._authenticated = True

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

    def capabilities(self) -> ServerCapabilities:
        """Lee las versiones y deduce la API disponible según el WebAPI.

        No sondea ``torrents/info`` ni ``getDirectoryContent`` y por tanto no
        demuestra permisos: solo qué endpoints define la versión del servidor.
        """
        version = self.version()
        webapi_version = self.webapi_version()
        components = _version_components(webapi_version)
        torrents = components[0] == 2
        directories = torrents and components >= _MIN_WEBAPI_DIRECTORY_METADATA
        return ServerCapabilities(
            version=version,
            webapi_version=webapi_version,
            torrents=torrents,
            directories=directories,
        )

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
