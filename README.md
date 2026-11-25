# qbt-elrocho — Gestión qBittorrent

Aplicación de escritorio para Windows y Linux que organizará los archivos
`.torrent` descargados a mano y los enviará a una instancia remota de
qBittorrent, con revisión y aprobación previas.

**Estado: prototipo en desarrollo.** La interfaz mantiene la revisión local de
`.torrent` y añade consultas de solo lectura a la Web API: versión, lista de
torrents y contenido del nivel inmediato de un directorio. No hay vigilancia,
persistencia, envío ni escritura remota.

No se envía ningún torrent en esta etapa. Antes de habilitar conexiones de
escritura se mantendrá la revisión y aprobación explícita por lote.

Desde Windows `10.0.4.20` se comprobó la WebUI y la API (`v5.3.0`). Tras limitar
el bypass anónimo a `10.0.4.20/32`, Windows leyó ese valor activo y el nodo
proxmox1 recibió HTTP 403. Los archivos de configuración de la VM aún muestran
`/24`, por lo que la persistencia tras reiniciar no está confirmada. No se
modificó el cortafuegos; el detalle está en la documentación del Vault. La
prueba de la interfaz contra el servidor real queda pendiente.

## Interfaz

La ventana mantiene dos secciones verticales: arriba la **revisión local** y
abajo la **consulta remota**, que se organiza en dos pestañas. El panel remoto
tiene un marco propio, títulos de pestaña ampliados y mayor separación respecto
a la cabecera de conexión; Revisión local conserva su estilo.

- **Revisión local (superior):** selección de `.torrent` y clasificación
  heurística en una tabla independiente de las pestañas. No abre conexiones ni
  envía archivos.
- **Cabecera común de qBittorrent:** URL editable solo cuando no hay trabajo
  (por defecto `http://10.0.4.56:8080`), botón «Consultar versión» y botón
  «Cancelar consulta». Los mensajes de «Consultar versión» aparecen en la
  etiqueta común bajo la URL.
- **Pestaña «Torrents»:** botón «Consultar torrents», su etiqueta de estado y
  la tabla de solo lectura con nombre, estado traducido, progreso y destino
  (`save_path`). La tabla no se puede editar.
- **Pestaña «Archivos descargados»:** rutas configurables (por defecto
  `/mnt/descargas/peliculas`, `/mnt/descargas/series` y
  `/mnt/descargas/libros`), selector de directorio, botón «Consultar
  directorio» —que pide explícitamente el nivel inmediato, sin recursividad—,
  su propia etiqueta de estado y la tabla de archivos y carpetas.
- **Mensajes por pestaña:** cada consulta escribe su estado (ocupado, éxito,
  error, cancelación y validación de ruta) en la etiqueta de la pestaña donde
  se lanzó. El contexto se captura al iniciar la consulta, no al recibir la
  respuesta, así que los mensajes siguen apareciendo en su pestaña aunque el
  usuario cambie de pestaña mientras la consulta está en curso. Las dos
  pestañas tienen tablas separadas: una consulta no vacía, rellena ni
  modifica la tabla de la otra.
- **Errores:** se muestran como texto plano (nunca se interpreta HTML, aunque
  la ruta remota contenga `<` o `&`). Un `403` se informa como acceso no
  autorizado. Si falla una consulta se conservan los últimos datos válidos de
  esa misma pestaña y se avisa de ello; si esa tabla no tenía datos, no se
  afirma nada sobre conservación y queda como estaba.
- **URL:** admite `http(s)://host[:puerto]` y un prefijo de ruta opcional.
  Se rechazan IPv6 mal formadas (por ejemplo
  `http://[::1`), puertos no numéricos o fuera de 1–65535, credenciales
  embebidas (`usuario:clave@`), query y fragmento. El error se muestra en la
  etiqueta de la consulta que se intentó iniciar y no se envía ninguna petición.
- **Sin conexión automática:** la ventana no consulta nada al arrancar. No se
  guardan credenciales ni claves; la URL no se persiste.

### Ancho de columnas

Las tres tablas permiten ajustar todas sus columnas arrastrando los separadores
de la cabecera. Los anchos elegidos se conservan al consultar de nuevo y al
cambiar el tamaño de la ventana, y también se guardan al cerrar la aplicación
para restaurarlos la próxima vez que se abra. Si el fichero de preferencias
estuviera corrupto se usan los anchos de fábrica, y si no se puede escribir al
cerrar se avisa con un diálogo sin impedir la salida.

Solo se persisten esos anchos, por tabla: la URL, las rutas, las credenciales y
los torrents mostrados no se guardan. El fichero es
`~/.config/el-rocho/qbt-elrocho.ini` (formato INI, ámbito de usuario). Los datos
siguen siendo de solo lectura.

### Capturas

Las imágenes de `docs/capturas/` se generaron con datos **DEMO** (ficticios) y
sin red: `qbt_ui_pestana_torrents.png` y `qbt_ui_pestana_archivos.png`. No son
lecturas de un servidor real.

## Límites y seguridad

La fase es exclusivamente de lectura: no hay `POST`, envío de torrents,
vigilancia de carpetas, SQLite ni almacenamiento de secretos.

- Tiempo de espera de cada operación de socket: **6 s de inactividad**, no un
  plazo total. Es el `timeout` de `urllib`: si la conexión avanza lentamente
  (por ejemplo, un servidor que envía bytes gota a gota), la consulta puede
  durar más de 6 s; no hay un límite duro de tiempo por consulta. La interfaz
  sigue respondiendo mientras espera.
- Tamaño máximo de respuesta: **2 000 000 bytes**.
- Máximo de entradas por respuesta: **2 000** (torrents o entradas de
  directorio). Al superarse se devuelve un error claro, no una lista truncada.
- La consulta de directorio usa `app/getDirectoryContent` con
  `withMetadata=true` y **solo lista el nivel inmediato**, tal como define la
  API oficial (`src/webui/api/appcontroller.cpp`); no se inventan campos ni
  recursividad.
- Las respuestas vacías, el JSON malformado, las listas con forma incorrecta y
  los textos de versión no válidos se tratan como error, nunca como éxito. Una
  lista JSON vacía sí es un resultado válido (servidor accesible sin torrents o
  directorio vacío). El `progress` de cada torrent debe ser un número finito
  entre 0 y 1, y el `type` de cada entrada de directorio debe ser el texto
  `dir` o `file`; en caso contrario la respuesta se rechaza como error.
- Cancelar marca la consulta para descartar su resultado con una señal
  cooperativa; no se usa `QThread::terminate()` ni se destruye el hilo. El
  trabajo real no se interrumpe: la ventana **sigue ocupada** hasta que el hilo
  termina y no se permite lanzar otra consulta entretanto, de modo que no se
  acumulan trabajos en el grupo de hilos. La interfaz sigue respondiendo.
- Cerrar con una consulta en curso **no bloquea la interfaz ni usa
  `waitForDone`**: el cierre se difiere (`event.ignore()`), se cancela el
  trabajo y la ventana se cierra cuando llega la señal de finalización. No hay
  un plazo duro para ese cierre: si la consulta se alarga, la ventana espera
  (deshabilitada, con estado «Cerrando…») hasta que el hilo termina; nunca se
  deja un hilo huérfano ni se fuerza su destrucción.
- Cada ventana usa su propio `QThreadPool` con un solo hilo; la interfaz no se
  bloquea y se ignora una segunda consulta mientras hay una activa.
- Los mensajes de error no incluyen la clave ni la URL completa.

## Base tecnológica prevista

- Python ≥ 3.11.
- Interfaz de escritorio con PySide6.
- Catálogo local en SQLite (no implementado).
- Comunicación con qBittorrent mediante su API Web (solo lectura en esta fase).

## Estructura

```
src/qbt/      interfaz, clasificación y cliente API de solo lectura
tests/        pruebas del clasificador, del cliente y de la interfaz Qt
docs/capturas capturas de la interfaz con datos DEMO (ficticios, sin red)
```

## Pruebas

Las pruebas usan un servidor HTTP local estricto: solo responden las rutas y
consultas registradas por cada prueba; cualquier otra ruta devuelve `404` y los
métodos distintos de `GET` devuelven `405`. No se contacta el servidor real.

```bash
env -u PYTHONPATH \
  LD_LIBRARY_PATH=/home/hermes/.hermes/tools/headless-egl \
  QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -v
```

En un escritorio con `libEGL.so.1` disponible basta con
`QT_QPA_PLATFORM=offscreen` y sin `LD_LIBRARY_PATH`. El stub de EGL de esta
máquina headless solo aporta esa biblioteca; no se instala nada en el sistema.

## Documentación

La documentación funcional y el seguimiento están en el Vault Obsidian, en
`Gestión qBittorrent/` del perfil `default`.
