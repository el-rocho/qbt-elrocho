# Instalación y configuración

Esta guía corresponde a la generalización, **fase 1 del árbol de desarrollo**.
No incluye todavía autenticación desde la interfaz. La aplicación no envía
torrents, no inicia descargas y no cambia preferencias del servidor.

## Instalar y abrir

Requisitos: Python 3.11 o posterior y un escritorio con las bibliotecas de Qt.
Desde la raíz de una copia del repositorio:

### Windows — PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\qbt-elrocho.exe
```

### Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/qbt-elrocho
```

En Linux pueden ser necesarios paquetes de sistema para Qt y `python3-venv`,
según la distribución. Un error de plataforma gráfica o de biblioteca EGL/XCB
no es un fallo de conexión con qBittorrent. No ejecute la aplicación como root.

Para desarrollar con los cambios del repositorio sin reinstalar en cada edición,
use `python -m pip install -e .` con el intérprete del entorno virtual.
Esta fase se comprueba automáticamente en Linux offscreen; la comprobación manual
en Windows sigue pendiente. Aún no se distribuyen instaladores autónomos.

## Primer inicio

La aplicación abre el diálogo **Configurar qBittorrent**. No se realiza ninguna
petición de red ni se crean directorios de descarga al abrirlo.

1. Elija **En este ordenador**, **En otro ordenador o contenedor** o
   **Continuar sin conexión**.
2. Para una conexión, escriba la URL de la **WebUI**, no una ruta de archivos ni
   un enlace de un torrent. Incluya `http://` o `https://` y el puerto que haya
   configurado en qBittorrent. La sugerencia local `http://localhost:8080` no es
   una dirección detectada ni una garantía de que el servicio esté disponible.
3. **Probar conexión** consulta únicamente la versión, sin guardar el borrador
   ni consultar torrents o directorios. Un resultado correcto comprueba ese
   endpoint, no todas las funciones ni los permisos sobre las carpetas.
4. Active o desactive **Consultar automáticamente al iniciar**.
5. Pulse **Guardar**. Se conserva la configuración, sin lanzar el lote inicial en
   ese momento. Puede consultar desde las pestañas o reabrir la aplicación.

**Configurar más tarde**, Esc y el aspa cierran el diálogo sin guardar. Puede
seguir revisando archivos locales; si no completó la configuración, la bienvenida
reaparecerá en el próximo inicio. **Continuar sin conexión + Guardar** es una
preferencia persistente: no hay consulta inicial y los botones remotos quedan
deshabilitados. No elimina la URL ni los directorios que ya estuvieran guardados.

El botón **Configuración**, en la cabecera, permite volver al diálogo. La URL de
la cabecera conserva la edición rápida y guarda sus valores válidos; no sustituye
la confirmación inicial mediante **Guardar** ni cambia el modo sin conexión.

## Valores que debe elegir el usuario

| Valor | Cómo elegirlo |
|---|---|
| Ubicación | Es orientativa; no instala qBittorrent ni descubre procesos o contenedores. |
| URL | Copie la dirección base de la WebUI que realmente utiliza. No añada `/api/v2`, usuario, contraseña, parámetros ni fragmentos. Un prefijo de proxy es admisible. |
| Puerto | Debe coincidir con el de la WebUI o con el publicado por el proxy/contenedor; no es el puerto de conexiones entre pares. |
| Consulta al iniciar | Se aplica en la próxima apertura, solo con configuración completada y fuera del modo sin conexión. |
| Directorios | Opcionales. Use rutas absolutas tal como las ve el proceso qBittorrent. |
| Tipo de directorio | Película, Serie, Libro, Otros o Sin determinar. Este último sirve como respaldo. |

En qBittorrent debe estar habilitada la WebUI y su dirección debe ser accesible
para la aplicación. Compruebe sus ajustes y pruebe la dirección en su navegador.
**La sesión del navegador no se comparte con esta aplicación.**

### Autenticación: límite de esta fase

Todavía no hay campos de usuario, contraseña ni clave API en la interfaz. Si el
servidor exige autenticación, esta fase no podrá consultarlo. **No desactive sus
protecciones ni amplíe listas blancas para eludir esta limitación**: continúe sin
conexión hasta la fase de autenticación. No introduzca secretos en la URL.
La aplicación no configura el servidor, el cortafuegos ni los certificados.

## Directorios de almacenamiento

Abra **Archivos descargados → Configurar directorios → Añadir**. Escriba la ruta,
elija su tipo y pulse **Aceptar**. Las nuevas instalaciones empiezan con una lista
vacía; las existentes conservan sus rutas guardadas. No es obligatorio dividir
el almacenamiento por categorías.

Ejemplos orientativos, no valores preinstalados:

| Entorno de qBittorrent | Ejemplo de destino |
|---|---|
| Windows local | `C:\Usuarios\Ana\Descargas` o una ruta real de otra unidad |
| Linux local o remoto | `/home/ana/Descargas` |
| Contenedor | `/downloads`, solo si esa es la ruta montada dentro del contenedor |

La carpeta donde selecciona los `.torrent` pertenece al ordenador de la
aplicación. El destino de descarga pertenece al entorno de qBittorrent. En un
contenedor, la ruta interna puede ser distinta de la del anfitrión aunque la
WebUI se abra con `localhost`. No convierta automáticamente una ruta en otra.

El diálogo valida el formato absoluto, no la existencia ni los permisos de
escritura. **Consultar directorios** solo lee el nivel inmediato; no comprueba
si qBittorrent puede escribir. La aplicación no crea carpetas.

Sin directorios configurados puede consultar torrents y revisar archivos. Los
destinos del catálogo anterior siguen siendo candidatos; eliminar una raíz no
borra los destinos de las filas. **Todavía no se separan por servidor**: revise
los destinos manualmente si cambia de instalación.

## Qué se conserva y cómo actualizar

Se mantienen tres archivos separados del perfil del usuario:

- `qbt-elrocho.json`: URL, directorios, modo, confirmación de configuración y
  preferencia de consulta inicial. No contiene secretos.
- `qbt-elrocho.sqlite3`: catálogo local y correcciones; no contiene resultados
  de consultas remotas.
- `qbt-elrocho.ini`: anchos de columnas.

La ubicación sigue siendo la de QSettings para el usuario. En Linux normalmente
es `~/.config/el-rocho/`; puede variar con el entorno. La ayuda emergente del botón **Configuración** muestra la ruta efectiva del JSON y la del estado local
muestra la ubicación del catálogo. En Windows use esas rutas efectivas, sin
suponer un nombre de usuario ni una unidad concreta.

La configuración JSON versión 1 se lee sin reescribirla al abrir. Si tenía URL
válida, conserva el arranque automático y sus directorios, incluso si la lista
está vacía. Al guardar un ajuste se escribe la versión 2 de forma atómica. Si la
versión anterior no tenía URL, se solicita configuración: no se reconstruye una
IP a partir de antiguos valores embebidos.

Antes de actualizar o volver a una versión anterior, haga una copia de los tres
archivos con la aplicación cerrada. Las versiones antiguas pueden no admitir
el JSON versión 2: restaure la copia compatible, no cambie el número de versión
a mano. No comparta su catálogo sin revisar rutas y metadatos personales.

## Si algo falla

- **No conecta:** compruebe que qBittorrent está iniciado, la WebUI habilitada y
  la URL/puerto correctos. Para otro equipo, revise la conectividad de red.
- **401/403:** puede requerirse autenticación u otra autorización. Esta fase no
  ofrece login. No implica necesariamente una contraseña errónea.
- **404 o respuesta no válida:** revise URL y prefijo del proxy. No se comprueba
  todavía la compatibilidad de cada función antes de consultar.
- **Configuración ilegible, incompatible o URL guardada inválida:** se muestra
  un aviso persistente, no se inicia ninguna consulta automática y el archivo
  queda protegido contra guardados desde la interfaz; «Configurar directorios»
  queda deshabilitado. Cierre la aplicación,
  haga una copia y restaure una configuración compatible. Si desea empezar de
  nuevo, aparte únicamente el JSON y reabra; no borre el catálogo ni el INI.
- **Fallo al guardar:** el diálogo permanece abierto, el aviso indica que no se
  guardó y la configuración en memoria no se sustituye por el borrador.
- **Consulta en curso:** cancelar descarta el resultado, pero espera a que termine
  la petición. Cerrar la ventana también espera sin destruir el trabajador.

En esta fase los lotes de consultas aún usan progreso modal y un aviso agregado
al terminar. El progreso no modal, la autenticación, la detección de capacidades,
la importación de la carpeta del servidor y el aislamiento de destinos por
servidor son fases posteriores, no funciones ya entregadas.

[Volver al README](../README.md)
