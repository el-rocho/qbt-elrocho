# qbt-elrocho — Gestión qBittorrent

Aplicación de escritorio para Windows y Linux que organizará los archivos
`.torrent` descargados a mano y los enviará a una instancia remota de
qBittorrent, con revisión y aprobación previas.

**Estado: prototipo en desarrollo.** La interfaz inicial permite seleccionar `.torrent` y revisar una clasificación heurística. El núcleo ya incluye un cliente API de solo lectura para consultar la versión, torrents y directorios; no hay vigilancia, persistencia, integración visual de la API ni envío implementados.

No se envía ningún torrent en esta etapa. Antes de habilitar conexiones de escritura se mantendrá la revisión y aprobación explícita por lote.

Desde Windows `10.0.4.20` se comprobó la WebUI y la API (`v5.3.0`). Tras limitar el bypass anónimo a `10.0.4.20/32`, Windows leyó ese valor activo y el nodo proxmox1 recibió HTTP 403. Los archivos de configuración de la VM aún muestran `/24`, por lo que la persistencia tras reiniciar no está confirmada. No se modificó el cortafuegos; el detalle está en la documentación del Vault.


## Base tecnológica prevista

- Python ≥ 3.11.
- Interfaz de escritorio con PySide6.
- Catálogo local en SQLite.
- Comunicación con qBittorrent mediante su API Web.

El paquete incluye clasificación inicial y un cliente de consulta API; catálogo, vigilancia, persistencia y envío aún no están implementados.

## Estructura

```
src/qbt/      interfaz inicial, clasificación y cliente API de solo lectura
tests/        pruebas automatizadas del clasificador y del cliente
```

## Documentación

La documentación funcional y el seguimiento están en el Vault Obsidian, en
`Gestión qBittorrent/` del perfil `default`.
