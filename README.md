# Grabador de Directos

App de escritorio para Windows que graba directos (YouTube, Twitch y todo lo que soporte
streamlink) y descarga vídeos normales, con vista previa en vivo y grabaciones programadas.

## Cómo funciona

| Caso | Motor |
|---|---|
| Directo | **streamlink** (igual que tu comando) → tubería → **ffmpeg**, que corta los archivos en fotogramas clave |
| Vídeo normal | **yt-dlp** (calidad elegida + ffmpeg para unir audio/vídeo y dividir) |
| Vista previa | **libmpv** embebido (variante de ≤480p para no gastar ancho de banda) o miniatura si es un vídeo |

Equivalencias con tu comando:

| Tu comando | En la app |
|---|---|
| `"1080p,best"` | Calidad **1080p** (si no existe, coge la mejor disponible por debajo y, si no hay ninguna, `best`) |
| `--retry-streams 30 --retry-max 60` | "Si no emite, reintentar cada **30** s, máx. **60** veces" |
| `--stream-segmented-duration` | **Duración fija** (h/min) o **Hasta que la pare** |
| `-o C:\grabaciones\Manana-%FECHA%.ts` | Carpeta + Nombre; se añade `_AAAA-MM-DD_HH-MM-SS` a cada archivo |
| *(nuevo)* | **Dividir en archivos de N min** — p. ej. 4 h en trozos de 30 min = 8 archivos |

Si el directo se corta, la app se reconecta sola y sigue en un archivo nuevo.

**Tope de seguridad:** con *Grabar ahora* + *Hasta que la pare*, la grabación se para sola a las
**5 horas**. A las 4 h 50 min aparece un aviso: *"Sí, 5 horas más"* la amplía; *"No"* (opción por
defecto) o no contestar la termina a las 5 h. Las grabaciones programadas no tienen este tope.

## Compilar el .exe con GitHub Actions

1. Crea un repositorio (p. ej. `grabador-directos`) y sube esta carpeta tal cual.
2. Cada `push` a `main` lanza el workflow **Compilar .exe (Windows)**.
   Descárgalo en **Actions → (última ejecución) → Artifacts → GrabadorDirectos-win64**.
3. Para una versión "oficial": `git tag v1.0 && git push --tags` → aparece en **Releases** como zip.

El workflow descarga automáticamente `ffmpeg.exe`, `libmpv-2.dll` y `deno.exe` (el motor
JavaScript que yt-dlp necesita para YouTube), así que no hay que instalar nada en el PC.
Descomprime el zip donde quieras y abre `GrabadorDirectos.exe`.

> Se compila en modo carpeta (no en un único .exe) porque arranca al instante. Con un solo
> .exe, cada arranque tendría que descomprimir ~300 MB.

## Programar grabaciones

- Botón **Programar…**: usa los ajustes del formulario y pide hora y repetición
  (una vez / todos los días / lunes a viernes / días concretos).
- Funciona **mientras la app esté abierta** (puede estar minimizada).
- Si la app se abre tarde dentro de una franja, graba lo que queda (p. ej. franja 7:00–10:00
  y abres a las 7:30 → graba hasta las 10:00). Las franjas que cruzan la medianoche funcionan.
- Para programas diarios en YouTube usa el enlace del canal acabado en **/live**
  (`https://www.youtube.com/@canal/live`), porque cada directo nuevo tiene una URL distinta.

## Formatos

- **TS** (recomendado): aguanta cortes de luz o cierres bruscos sin perder el archivo.
- **MKV / MP4**: sin recodificar. MP4 puede quedar dañado si se apaga el PC a mitad.
- **MP3**: solo audio, recodificado a 192 kbps. **M4A**: solo audio, copia directa (sin pérdida).

## Datos

Ajustes y programaciones: `%APPDATA%\GrabadorDirectos\` (`config.json`, `programaciones.json`).

## Ejecutar sin compilar (desarrollo)

```
pip install -r requirements.txt
python run.py
```
(ffmpeg en el PATH; para la vista previa en vivo, `libmpv-2.dll` en una carpeta `bin\` junto a `run.py`.)
