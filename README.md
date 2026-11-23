# qbt-elrocho — Gestión qBittorrent

Aplicación de escritorio para Windows y Linux que organizará los archivos
`.torrent` descargados a mano y los enviará a una instancia remota de
qBittorrent, con revisión y aprobación previas.

**Estado: proyecto preparado, desarrollo no iniciado.** El repositorio contiene
sólo el esqueleto (empaquetado, configuración de Git y documentación mínima).
El alcance y las decisiones de diseño se concretarán antes de escribir código.

## Base tecnológica prevista

- Python ≥ 3.11.
- Interfaz de escritorio con PySide6.
- Catálogo local en SQLite.
- Comunicación con qBittorrent mediante su API Web.

Estas son las premisas indicadas por el usuario; ningún módulo está implementado.

## Estructura

```
src/qbt/      paquete de la aplicación (por ahora, sólo la versión)
```

## Documentación

La documentación funcional y el seguimiento están en el Vault Obsidian, en
`Gestión qBittorrent/` del perfil `default`.
