# qbt-elrocho — Gestión qBittorrent

Aplicación de escritorio para Windows y Linux que organizará los archivos
`.torrent` descargados a mano y los enviará a una instancia remota de
qBittorrent, con revisión y aprobación previas.

**Estado: prototipo en desarrollo.** La interfaz mantiene la revisión local de
`.torrent` y añade consultas de solo lectura a la Web API: versión, lista de
torrents y contenido del nivel inmediato de los directorios configurados.
Conserva en SQLite la revisión local, sus correcciones y destinos elegidos, y en
un JSON aparte la URL y los directorios de descarga. No hay vigilancia, envío ni
escritura remota; las consultas del servidor siguen siendo temporales.

No se envía ningún torrent en esta etapa. Antes de habilitar conexiones de
escritura se mantendrá la revisión y aprobación explícita por lote.

Desde Windows `10.0.4.20` se comprobó la WebUI y la API (`v5.3.0`). El
25-11-2026 se corrigió la persistencia del bypass anónimo `10.0.4.20/32`:
el archivo de configuración del servidor tenía el atributo inmutable, que
impedía guardar los cambios. Tras retirarlo, aplicar la restricción y detener
y arrancar limpiamente el servicio, el archivo conserva `/32` y proxmox1
recibe HTTP 403 sin autenticación. No se modificó el cortafuegos ni el código
de la aplicación durante esa intervención. La prueba posterior desde Windows
fue confirmada sin pedir autenticación. Por decisión del administrador se volvió
a activar el atributo inmutable sobre la configuración corregida: para futuros
cambios de preferencias habrá que desbloquearlo temporalmente y verificar el
guardado antes de volver a protegerlo. El detalle operativo y las copias de
configuración permanecen en la documentación privada del Vault y en el servidor,
no en este repositorio.

## Interfaz

La ventana mantiene dos secciones verticales: arriba la **revisión local** y
abajo la **consulta remota**, que se organiza en dos pestañas. El panel remoto
tiene un marco propio, títulos de pestaña ampliados y mayor separación respecto
a la cabecera de conexión; Revisión local conserva su estilo.

- **Revisión local (superior):** selección y lectura de `.torrent`, clasificación
  heurística y catálogo SQLite. Permite corregir tipo, título y temporada,
  elegir un destino y marcar Incluir. Los archivos originales no se modifican,
  no se abren conexiones desde este panel y marcar una fila **no envía ni
  aprueba un lote**. Las tablas remotas continúan sin permitir edición.
- **Cabecera común de qBittorrent:** URL editable solo cuando no hay trabajo
  (por defecto `http://10.0.4.56:8080`), botón «Consultar versión» y botón
  «Cancelar consulta». Los mensajes de «Consultar versión» aparecen en la
  etiqueta común bajo la URL. La URL válida se guarda al editarla y se restaura
  al reabrir.
- **Pestaña «Torrents»:** botón «Consultar torrents», su etiqueta de estado y
  la tabla de solo lectura con nombre, estado traducido, progreso y destino
  (`save_path`). La tabla no se puede editar. Al abrir la aplicación se consulta
  automáticamente; si la conexión falla se muestra un aviso modal (también al
  pulsar el botón manualmente).
- **Pestaña «Archivos descargados»:** botón «Configurar directorios», que abre
  un diálogo para definir y ajustar todas las rutas de descarga y su tipo
  (Película, Serie, Libro, Otros o «Sin determinar» como comodín). Botón
  «Consultar directorios», que consulta **todos** los directorios configurados
  de una vez; cada uno se muestra en su propia pestaña interna con la tabla de
  archivos y carpetas del nivel inmediato. La lectura **no es recursiva**: solo
  se lista el nivel inmediato de cada ruta.
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
- **Consulta inicial:** al abrir, la aplicación consulta automáticamente los
  torrents y todos los directorios configurados, mostrando un diálogo modal de
  progreso mientras se ejecutan las llamadas secuenciales y, al terminar, un
  único aviso agregado con el resultado (o los errores) de cada consulta. No se
  guardan credenciales ni claves.

### Ancho de columnas

Las tres tablas permiten ajustar todas sus columnas arrastrando los separadores
de la cabecera. Los anchos elegidos se conservan al consultar de nuevo y al
cambiar el tamaño de la ventana, y también se guardan al cerrar la aplicación
para restaurarlos la próxima vez que se abra. Si el fichero de preferencias
estuviera corrupto se usan los anchos de fábrica, y si no se puede escribir al
cerrar se avisa con un diálogo sin impedir la salida.

En las preferencias INI solo se persisten los anchos, por tabla. En Linux el
fichero es `~/.config/el-rocho/qbt-elrocho.ini`; en Windows, la ubicación INI de
QSettings para el usuario. La URL y los directorios configurados se guardan
aparte, en `qbt-elrocho.json` junto al INI. Las credenciales y las consultas
remotas no se guardan. El catálogo local SQLite es independiente y sí conserva
los destinos de las filas revisadas.

### Catálogo, destinos y duplicados

- **Guardado local:** cada alta o corrección válida se guarda inmediatamente.
  Al abrir se recuperan las revisiones, no las consultas remotas. La base
  `qbt-elrocho.sqlite3` está junto al INI; su ruta aparece en la ayuda emergente
  del mensaje del panel local. No contiene el archivo torrent ni sus trackers,
  claves o credenciales; sí rutas de origen, identidades y metadatos revisados.
- **Correcciones:** Tipo es un selector; Título y Temporada se editan con doble
  clic o F2. La clasificación parte del nombre del archivo y es conservadora:
  no decide que un nombre desconocido sea una película. Los cambios manuales
  quedan identificados como tales.
- **Destinos por usuario:** cada fila permite seleccionar o escribir su destino
  absoluto; también ofrece los directorios configurados de su tipo y los
  destinos ya guardados del mismo tipo. Películas y libros proponen su
  directorio, sin crear carpetas por título o autor. Cuando un tipo tiene
  varias alternativas conocidas, no se elige una automáticamente; la excepción
  es que el título ya exista en un destino guardado o, para series, como carpeta
  en los listados leídos al inicio, en cuyo caso se reutiliza esa ubicación.
  El tipo «Otros» agrupa lo que no es película, serie ni libro; un directorio
  «Sin determinar» actúa como respaldo cuando no hay uno del tipo concreto. La
  aplicación no añade subcarpetas de serie/temporada: la estructura del torrent
  y la configuración del servidor siguen mandando.
- **Identidad exacta:** calcula el infohash sobre los bytes originales del
  diccionario `info`: SHA1 en v1, SHA256 en v2 y ambas identidades en híbridos.
  Archivos con distinto nombre pero la misma identidad son duplicados. La
  segunda revisión se desmarca por defecto, sin borrar nada. También se detecta
  la identidad de registros cuyo archivo original está ausente o en error:
  una copia válida puede quedar desmarcada por coincidir con uno de ellos.
  No hay sustitución ni limpieza automática del catálogo; se puede revisar
  manualmente Incluir en la copia válida.
- **Servidor:** tras pulsar «Consultar torrents» se compara con esa última
  respuesta; una coincidencia exacta desmarca la fila, **aunque se hubiese
  marcado manualmente antes de consultar**. Cada nueva consulta vuelve a aplicar
  esa exclusión; se puede volver a revisar la casilla después. La respuesta no se
  almacena en SQLite, aunque la selección local resultante sí queda guardada.
  Cambiar URL descarta esa comparación y no vuelve a marcar filas por sí solo.
  Sin consultar no se afirma que el torrent falte en el servidor. Para v2 se
  necesita el identificador completo; un hash remoto truncado no se trata
  como prueba de coincidencia.
- **Títulos parecidos:** son solo avisos de posible contenido relacionado,
  nunca una prueba ni motivo para excluir automáticamente. No se comparan los
  bytes del contenido descargado, ni se recorre la biblioteca. La consulta de
  directorio continúa siendo una vista independiente del nivel inmediato.
- **Errores:** archivos inválidos, ausentes o sustituidos quedan señalados y
  excluidos. Se vuelven a comprobar los originales al abrir; reañadir la misma
  identidad conserva correcciones; si no se puede leer, conserva igualmente
  las correcciones y la última identidad conocida, pero se excluye y muestra
  el error. Una identidad distinta y válida exige revisar una nueva propuesta.
  Una base incompatible no se sobrescribe. Si no se puede
  guardar, se muestra el fallo y se conserva el último valor guardado; el cierre
  tampoco oculta un rechazo de validación al consolidar el editor activo.
- **Límites locales:** archivo torrent de hasta 8 MiB, profundidad bencode 32
  y 100 000 valores. Se comprueba estructura, no la integridad criptográfica
  completa de las piezas. La lectura local, SQLite y la comparación se ejecutan
  en el hilo de interfaz: grandes catálogos o archivos en unidades lentas
  pueden introducir pausas. El trabajo de red sigue en segundo plano.
- **Conservación:** la base mantiene el último estado por ruta de origen hasta
  que el usuario gestione su copia local; aún no hay borrado desde la interfaz,
  caducidad, auditoría de cada cambio ni historial de envíos.

### Capturas

Las imágenes de `docs/capturas/` se generaron con datos **DEMO** (ficticios) y
sin red. `qbt_ui_pestana_torrents.png` y `qbt_ui_pestana_archivos.png`
corresponden a la fase anterior; `qbt_ui_catalogo_local.png` y
`qbt_ui_catalogo_directorio.png` muestran la revisión local con destinos y
avisos. No son lecturas de un servidor real.

## Límites y seguridad

El acceso al servidor es exclusivamente de lectura: no hay `POST`, envío de
torrents, cambios de preferencias ni vigilancia de carpetas. SQLite solo
almacena la revisión local, sin credenciales ni respuestas remotas.

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
  bloquea y se ignora una segunda consulta mientras hay una activa. Las
  consultas de directorio y la consulta inicial se ejecutan en secuencia, una a
  una, con un diálogo modal de progreso y un único aviso agregado al terminar
  (los errores idénticos se deduplican).
- Los mensajes de error no incluyen la clave ni la URL completa.

## Base tecnológica prevista

- Python ≥ 3.11.
- Interfaz de escritorio con PySide6.
- Catálogo local en SQLite (biblioteca estándar).
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
