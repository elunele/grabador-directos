"""
Núcleo del Grabador de Directos.

- Directos: streamlink (como tu comando) → tubería → ffmpeg (muxer "segment")
  ffmpeg corta los archivos en fotogramas clave, así cada trozo se reproduce bien.
- Vídeos normales (no en directo): yt-dlp.
- Programador interno de grabaciones (funciona mientras la app está abierta).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

APP_NAME = "GrabadorDirectos"

# --------------------------------------------------------------------------- #
#  Rutas y binarios incluidos (ffmpeg.exe, libmpv-2.dll, deno.exe)
# --------------------------------------------------------------------------- #

def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def setup_bundled_paths() -> None:
    """Pone en el PATH la carpeta bin/ incluida (ffmpeg, libmpv, deno)."""
    candidates = [
        os.path.join(app_dir(), "bin"),
        app_dir(),
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "bin"),
        os.path.join(os.path.dirname(app_dir()), "bin"),
    ]
    for d in candidates:
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def load_json(name: str, default):
    try:
        with open(os.path.join(config_dir(), name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name: str, data) -> None:
    path = os.path.join(config_dir(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
#  Opciones
# --------------------------------------------------------------------------- #

QUALITIES = {
    # etiqueta: altura máxima (None = la mejor, 0 = la peor)
    "Mejor disponible": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "Mínima (ahorro de datos)": 0,
}

# etiqueta: (extensión, solo_audio, argumentos de códec, formato del muxer)
FORMATS = {
    "TS (recomendado, aguanta cortes)": ("ts", False, ["-c", "copy"], "mpegts"),
    "MKV": ("mkv", False, ["-c", "copy"], "matroska"),
    "MP4": ("mp4", False, ["-c", "copy"], "mp4"),
    "MP3 (solo audio)": ("mp3", True, ["-c:a", "libmp3lame", "-b:a", "192k"], "mp3"),
    "M4A (solo audio, sin recodificar)": ("m4a", True, ["-c:a", "copy"], "mp4"),
}

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, maxlen: int = 80) -> str:
    text = INVALID_CHARS.sub("_", text or "").strip(" .")
    return (text[:maxlen].strip(" .") or "grabacion")


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_duration(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --------------------------------------------------------------------------- #
#  Análisis del enlace (título, ¿directo?, miniatura, URL de vista previa)
# --------------------------------------------------------------------------- #

_session = None
_session_lock = threading.Lock()


def get_session():
    """Sesión de streamlink compartida (solo para analizar/vista previa)."""
    global _session
    with _session_lock:
        if _session is None:
            _session = new_streamlink_session()
        return _session


def new_streamlink_session():
    from streamlink import Streamlink
    s = Streamlink()
    ff = ffmpeg_path()
    if ff:
        s.set_option("ffmpeg-ffmpeg", ff)
    s.set_option("webbrowser", False)
    s.set_option("stream-timeout", 90)
    s.set_option("hls-live-edge", 3)
    return s


def youtube_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def pick_stream_name(names, max_height, audio_only=False):
    """Elige el nombre de stream de streamlink según la calidad pedida.
    Equivale a tu "1080p,best": la más alta <= límite y, si no, 'best'."""
    parsed = []
    for n in names:
        m = re.fullmatch(r"(\d+)p(\d+)?(_alt\d*)?", n)
        if m:
            parsed.append((int(m.group(1)), int(m.group(2) or 30), n))
    parsed.sort()
    if audio_only:
        if "audio_only" in names:
            return "audio_only"
        # En YouTube las variantes >=360p llevan el audio a 128 kbps
        ok = [p for p in parsed if p[0] >= 360]
        if ok:
            return ok[0][2]
        return "best" if "best" in names else (parsed[-1][2] if parsed else None)
    if max_height == 0:
        return "worst" if "worst" in names else (parsed[0][2] if parsed else None)
    if max_height is None:
        return "best" if "best" in names else (parsed[-1][2] if parsed else None)
    ok = [p for p in parsed if p[0] <= max_height]
    if ok:
        return ok[-1][2]
    return "best" if "best" in names else (parsed[-1][2] if parsed else None)


@dataclass
class LinkInfo:
    url: str
    kind: str = "desconocido"      # directo | proximo | video | desconocido
    title: str = ""
    channel: str = ""
    thumbnail: str | None = None
    preview_url: str | None = None  # HLS de baja calidad para la vista previa
    qualities: list = field(default_factory=list)
    error: str = ""


def analyze(url: str) -> LinkInfo:
    info = LinkInfo(url=url)
    yt_error = ""
    # 1) yt-dlp: metadatos, miniatura y si es directo o vídeo
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "logger": _SilentLogger()}
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
        info.title = d.get("title") or ""
        info.channel = d.get("channel") or d.get("uploader") or ""
        info.thumbnail = d.get("thumbnail")
        status = d.get("live_status")
        if d.get("is_live") or status == "is_live":
            info.kind = "directo"
        elif status == "is_upcoming":
            info.kind = "proximo"
        else:
            info.kind = "video"
            heights = sorted({f.get("height") for f in d.get("formats", []) if f.get("height")})
            info.qualities = [f"{h}p" for h in heights]
    except Exception as e:  # noqa: BLE001
        yt_error = str(e)
        low = yt_error.lower()
        if "will begin" in low or "premieres in" in low or "live event" in low:
            info.kind = "proximo"
    vid = youtube_id(url)
    if not info.thumbnail and vid:
        info.thumbnail = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    # 2) streamlink: si está emitiendo, sacamos calidades y URL de vista previa
    if info.kind in ("directo", "desconocido", "proximo"):
        try:
            streams = get_session().streams(url)
            if streams:
                info.kind = "directo"
                info.qualities = [n for n in streams if re.fullmatch(r"\d+p\d*", n)]
                name = pick_stream_name(list(streams), 480) or "worst"
                st = streams.get(name) or streams.get("worst")
                try:
                    info.preview_url = st.to_url()
                except Exception:  # noqa: BLE001
                    info.preview_url = getattr(st, "url", None)
        except Exception as e:  # noqa: BLE001
            if info.kind == "desconocido":
                info.error = str(e)
    if info.kind == "desconocido" and not info.error:
        info.error = yt_error or "No se encontró ningún directo ni vídeo en ese enlace."
    return info


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def download_bytes(url: str, timeout: int = 15) -> bytes | None:
    try:
        r = get_session().http.get(url, timeout=timeout)
        return r.content
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
#  Trabajo de grabación
# --------------------------------------------------------------------------- #

@dataclass
class JobConfig:
    url: str
    folder: str
    basename: str = "grabacion"
    quality: str = "1080p"
    fmt: str = "TS (recomendado, aguanta cortes)"
    duration_min: int = 0          # 0 = hasta que yo la pare
    split_min: int = 0             # 0 = un solo archivo
    retry_interval: int = 30       # --retry-streams 30
    retry_max: int = 60            # --retry-max 60  (0 = infinito)
    kind: str = "directo"          # directo | video
    end_at: float | None = None    # marca de tiempo fija de fin (programaciones)
    origin: str = "manual"


class Recording(threading.Thread):
    def __init__(self, cfg: JobConfig, log=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.id = uuid.uuid4().hex[:8]
        self.status = "Preparando"
        self.started_at = time.time()
        self.recorded_bytes = 0
        self.recording_seconds = 0.0
        self.files: list[str] = []
        self.error = ""
        self.finished = False
        self._stop = threading.Event()
        self._fd = None
        self._ffmpeg = None
        self._log_cb = log or (lambda *_: None)
        if cfg.end_at:
            self.deadline = cfg.end_at
        elif cfg.duration_min > 0:
            self.deadline = self.started_at + cfg.duration_min * 60
        else:
            self.deadline = None

    # ---- utilidades -------------------------------------------------------
    def log(self, msg: str):
        self._log_cb(f"[{datetime.now():%H:%M:%S}] [{self.cfg.basename}] {msg}")

    def stop(self):
        self._stop.set()
        fd = self._fd
        if fd is not None:
            try:
                fd.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def stopping(self) -> bool:
        return self._stop.is_set() or (self.deadline is not None and time.time() >= self.deadline)

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.time())

    def _output_pattern(self) -> tuple[str, str, bool, list, str]:
        ext, audio_only, codec, muxer = FORMATS[self.cfg.fmt]
        base = safe_name(self.cfg.basename)
        pattern = os.path.join(self.cfg.folder, base).replace("%", "%%") + f"_%Y-%m-%d_%H-%M-%S.{ext}"
        return pattern, ext, audio_only, codec, muxer

    # ---- hilo principal ---------------------------------------------------
    def run(self):
        try:
            os.makedirs(self.cfg.folder, exist_ok=True)
            if not ffmpeg_path():
                raise RuntimeError("No se encuentra ffmpeg.exe")
            if self.cfg.kind == "video":
                self._run_video()
            else:
                self._run_live()
            if not self.error:
                self.status = "Terminada"
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.status = "Error"
            self.log(f"ERROR: {e}")
        finally:
            self.finished = True
            self.log(f"Fin. {len(self.files)} archivo(s), {fmt_bytes(self.recorded_bytes)}")

    # ---- directos: streamlink → ffmpeg segment ---------------------------
    def _run_live(self):
        session = new_streamlink_session()
        pattern, ext, audio_only, codec, muxer = self._output_pattern()
        max_h = QUALITIES.get(self.cfg.quality, None)
        attempts = 0
        while not self.stopping:
            try:
                streams = session.streams(self.cfg.url)
            except Exception as e:  # noqa: BLE001
                self.log(f"No se pudo leer el directo: {e}")
                streams = {}
            if not streams:
                attempts += 1
                if self.cfg.retry_max and attempts > self.cfg.retry_max:
                    if self.recorded_bytes == 0:
                        raise RuntimeError("El directo no está emitiendo (se agotaron los reintentos)")
                    self.log("El directo terminó (se agotaron los reintentos).")
                    return
                tope = self.cfg.retry_max or "∞"
                self.status = f"Esperando emisión ({attempts}/{tope})"
                self.log(f"Sin emisión. Reintento en {self.cfg.retry_interval}s ({attempts}/{tope})")
                self._stop.wait(self.cfg.retry_interval)
                continue
            name = pick_stream_name(list(streams), max_h, audio_only)
            stream = streams[name]
            self.log(f"Grabando calidad «{name}»")
            t0 = time.time()
            self._record_once(stream, pattern, audio_only, codec, muxer)
            if self.stopping:
                break
            # Si aguantó más de 1 min grabando, contamos los reintentos desde cero.
            attempts = 0 if time.time() - t0 > 60 else attempts + 1
            if self.cfg.retry_max and attempts > self.cfg.retry_max:
                self.log("El directo terminó (se agotaron los reintentos).")
                return
            self.status = "Reconectando"
            self.log("Se cortó la emisión, reconectando…")
            self._stop.wait(min(5, self.cfg.retry_interval) if attempts <= 1 else self.cfg.retry_interval)

    def _record_once(self, stream, pattern, audio_only, codec, muxer):
        split = self.cfg.split_min * 60 if self.cfg.split_min > 0 else 10 ** 7
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
               "-fflags", "+genpts", "-i", "pipe:0"]
        cmd += ["-map", "0:a?"] if audio_only else ["-map", "0:v?", "-map", "0:a?", "-dn", "-sn"]
        if audio_only:
            cmd += ["-vn"]
        cmd += codec
        cmd += ["-f", "segment", "-segment_time", str(split), "-segment_format", muxer,
                "-reset_timestamps", "1", "-strftime", "1"]
        if muxer == "mp4":
            cmd += ["-segment_format_options", "movflags=+faststart"]
        cmd += [pattern]
        before = set(self._existing())
        self._ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        threading.Thread(target=self._pump_stderr, args=(self._ffmpeg,), daemon=True).start()
        t0 = time.time()
        try:
            self._fd = stream.open()
            self.status = "Grabando"
            while not self.stopping:
                data = self._fd.read(64 * 1024)
                if not data:
                    break
                self._ffmpeg.stdin.write(data)
                self.recorded_bytes += len(data)
                self._refresh_files(before)
        except (OSError, ValueError) as e:
            if not self._stop.is_set():
                self.log(f"Corte de lectura/escritura: {e}")
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.log(f"Error del stream: {e}")
        finally:
            self.recording_seconds += time.time() - t0
            try:
                if self._fd:
                    self._fd.close()
            except Exception:  # noqa: BLE001
                pass
            self._fd = None
            try:
                self._ffmpeg.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._ffmpeg.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self._ffmpeg.kill()
            self._refresh_files(before)

    def _existing(self):
        try:
            return [os.path.join(self.cfg.folder, f) for f in os.listdir(self.cfg.folder)]
        except OSError:
            return []

    def _refresh_files(self, before):
        now = time.time()
        if now - getattr(self, "_last_scan", 0) < 3:
            return
        self._last_scan = now
        prefix = safe_name(self.cfg.basename) + "_"
        for p in self._existing():
            if p not in before and os.path.basename(p).startswith(prefix) and p not in self.files:
                self.files.append(p)
                self.log(f"Nuevo archivo: {os.path.basename(p)}")

    def _pump_stderr(self, proc):
        for line in iter(proc.stderr.readline, b""):
            txt = line.decode("utf-8", "replace").strip()
            if txt:
                self.log(f"ffmpeg: {txt}")

    # ---- vídeos normales: yt-dlp -----------------------------------------
    def _run_video(self):
        import yt_dlp
        from yt_dlp.utils import download_range_func
        ext, audio_only, _codec, _muxer = FORMATS[self.cfg.fmt]
        max_h = QUALITIES.get(self.cfg.quality, None)
        if audio_only:
            fmt = "bestaudio/best"
        elif max_h is None:
            fmt = "bv*+ba/b"
        elif max_h == 0:
            fmt = "wv*+wa/w"
        else:
            fmt = f"bv*[height<={max_h}]+ba/b[height<={max_h}]/bv*+ba/b"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = os.path.join(self.cfg.folder, f"{safe_name(self.cfg.basename)}_{stamp}.%(ext)s")
        opts = {
            "format": fmt, "outtmpl": out, "noplaylist": True, "quiet": True,
            "no_warnings": True, "ffmpeg_location": ffmpeg_path(), "logger": _YtLogger(self),
            "progress_hooks": [self._yt_hook], "retries": 10, "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
        }
        if audio_only:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": ext,
                                       "preferredquality": "192"}]
        else:
            # yt-dlp no une en .ts: en ese caso usamos mkv (igual de robusto)
            opts["merge_output_format"] = ext if ext in ("mp4", "mkv") else "mkv"
        if self.cfg.duration_min > 0:
            opts["download_ranges"] = download_range_func(None, [(0, self.cfg.duration_min * 60)])
        self.status = "Descargando"
        self.log("Descargando vídeo con yt-dlp…")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([self.cfg.url])
        if self._stop.is_set():
            self.status = "Cancelada"
            return
        produced = [p for p in self._existing()
                    if os.path.basename(p).startswith(f"{safe_name(self.cfg.basename)}_{stamp}")
                    and not p.endswith((".part", ".ytdl"))]
        self.files = produced
        if self.cfg.split_min > 0 and produced:
            self._split_file(produced[0])

    def _yt_hook(self, d):
        if self._stop.is_set():
            raise yt_dlp_cancel()
        if d.get("status") == "downloading":
            self.recorded_bytes = d.get("downloaded_bytes") or self.recorded_bytes
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                self.status = f"Descargando {100 * self.recorded_bytes / total:.0f}%"

    def _split_file(self, path):
        self.status = "Dividiendo"
        root, ext = os.path.splitext(path)
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", path,
               "-map", "0", "-c", "copy", "-f", "segment",
               "-segment_time", str(self.cfg.split_min * 60), "-reset_timestamps", "1",
               f"{root}_parte%02d{ext}"]
        r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
        if r.returncode == 0:
            os.remove(path)
            prefix = os.path.basename(root) + "_parte"
            self.files = sorted(p for p in self._existing() if os.path.basename(p).startswith(prefix))
            self.log(f"Dividido en {len(self.files)} partes")
        else:
            self.log("No se pudo dividir: " + r.stderr.decode("utf-8", "replace")[-300:])


def yt_dlp_cancel():
    from yt_dlp.utils import DownloadCancelled
    return DownloadCancelled("Cancelada por el usuario")


class _YtLogger:
    def __init__(self, rec): self.rec = rec
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): self.rec.log(f"yt-dlp: {msg}")
    def error(self, msg): self.rec.log(f"yt-dlp: {msg}")


# --------------------------------------------------------------------------- #
#  Programador
# --------------------------------------------------------------------------- #

REPEATS = ["Una vez", "Todos los días", "Lunes a viernes", "Días concretos"]
DAY_NAMES = ["L", "M", "X", "J", "V", "S", "D"]


@dataclass
class Schedule:
    url: str
    folder: str
    basename: str
    quality: str
    fmt: str
    start: str                 # "YYYY-MM-DD HH:MM" (la fecha solo cuenta en "Una vez")
    duration_min: int          # 0 = hasta que la pare
    split_min: int = 0
    repeat: str = "Una vez"
    days: list = field(default_factory=list)   # 0=lunes … 6=domingo
    enabled: bool = True
    retry_interval: int = 30
    retry_max: int = 60
    done: list = field(default_factory=list)   # ocurrencias ya lanzadas
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def start_dt(self) -> datetime:
        return datetime.strptime(self.start, "%Y-%m-%d %H:%M")

    def occurrences_near(self, now: datetime):
        """Inicios de ocurrencia de ayer y hoy (cubre franjas que cruzan medianoche)."""
        sd = self.start_dt()
        if self.repeat == "Una vez":
            return [sd]
        res = []
        for delta in (1, 0):
            day = (now - timedelta(days=delta)).date()
            occ = datetime.combine(day, sd.time())
            if occ < sd.replace(second=0):  # no antes de la fecha de alta
                continue
            wd = occ.weekday()
            if self.repeat == "Lunes a viernes" and wd > 4:
                continue
            if self.repeat == "Días concretos" and wd not in self.days:
                continue
            res.append(occ)
        return res

    def window_min(self) -> int:
        # Si el PC/app se abre tarde, se graba lo que quede de la franja.
        return self.duration_min if self.duration_min > 0 else 10

    def next_run(self, now: datetime | None = None) -> datetime | None:
        now = now or datetime.now()
        sd = self.start_dt()
        if self.repeat == "Una vez":
            key = sd.strftime("%Y-%m-%d %H:%M")
            return None if key in self.done or sd + timedelta(minutes=self.window_min()) < now else sd
        for i in range(0, 8):
            day = (now + timedelta(days=i)).date()
            occ = datetime.combine(day, sd.time())
            if occ < sd or occ + timedelta(minutes=self.window_min()) < now:
                continue
            wd = occ.weekday()
            if self.repeat == "Lunes a viernes" and wd > 4:
                continue
            if self.repeat == "Días concretos" and wd not in self.days:
                continue
            if occ.strftime("%Y-%m-%d %H:%M") in self.done:
                continue
            return occ
        return None

    def describe(self) -> str:
        hora = self.start_dt().strftime("%H:%M")
        if self.repeat == "Una vez":
            cuando = self.start_dt().strftime("%d/%m/%Y %H:%M")
        elif self.repeat == "Días concretos":
            cuando = ",".join(DAY_NAMES[d] for d in sorted(self.days)) + f" {hora}"
        else:
            cuando = f"{self.repeat} {hora}"
        dur = f"{self.duration_min // 60}h{self.duration_min % 60:02d}" if self.duration_min else "hasta parar"
        cada = f", cortes {self.split_min} min" if self.split_min else ""
        return f"{cuando} · {dur}{cada}"


class Scheduler(threading.Thread):
    """Comprueba cada 15 s si toca lanzar alguna programación."""

    def __init__(self, launch_cb, log=None):
        super().__init__(daemon=True)
        self.launch_cb = launch_cb
        self.log = log or (lambda *_: None)
        self.lock = threading.Lock()
        self.items: list[Schedule] = []
        self._stop = threading.Event()
        self.load()

    def load(self):
        raw = load_json("programaciones.json", [])
        items = []
        for d in raw:
            try:
                items.append(Schedule(**d))
            except TypeError:
                pass
        self.items = items

    def save(self):
        with self.lock:
            save_json("programaciones.json", [asdict(s) for s in self.items])

    def add(self, s: Schedule):
        with self.lock:
            self.items.append(s)
        self.save()

    def remove(self, sid: str):
        with self.lock:
            self.items = [s for s in self.items if s.id != sid]
        self.save()

    def toggle(self, sid: str):
        with self.lock:
            for s in self.items:
                if s.id == sid:
                    s.enabled = not s.enabled
        self.save()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick(datetime.now())
            except Exception as e:  # noqa: BLE001
                self.log(f"Programador: {e}")
            self._stop.wait(15)

    def tick(self, now: datetime):
        to_launch = []
        with self.lock:
            for s in self.items:
                if not s.enabled:
                    continue
                for occ in s.occurrences_near(now):
                    key = occ.strftime("%Y-%m-%d %H:%M")
                    if key in s.done:
                        continue
                    if occ <= now < occ + timedelta(minutes=s.window_min()):
                        s.done = (s.done + [key])[-50:]
                        end_at = (occ + timedelta(minutes=s.duration_min)).timestamp() \
                            if s.duration_min > 0 else None
                        to_launch.append((s, end_at))
        if to_launch:
            self.save()
        for s, end_at in to_launch:
            cfg = JobConfig(url=s.url, folder=s.folder, basename=s.basename, quality=s.quality,
                            fmt=s.fmt, duration_min=s.duration_min, split_min=s.split_min,
                            retry_interval=s.retry_interval, retry_max=s.retry_max,
                            kind="directo", end_at=end_at, origin="programada")
            self.log(f"Programación «{s.basename}» lanzada")
            self.launch_cb(cfg)
