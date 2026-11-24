"""Grabador de Directos — interfaz gráfica (customtkinter)."""
from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import core  # type: ignore
else:
    from . import core

core.setup_bundled_paths()

import customtkinter as ctk  # noqa: E402
from PIL import Image  # noqa: E402

try:  # vista previa en vivo (libmpv-2.dll)
    import mpv  # type: ignore
    MPV_OK = True
except Exception:  # noqa: BLE001
    mpv = None
    MPV_OK = False

VERSION = "1.1"
AUTO_LIMIT_MIN = 5 * 60       # "Grabar ahora" + "Hasta que la pare": tope de 5 h
WARN_BEFORE_MIN = 10          # aviso a las 4 h 50 min
EXTEND_MIN = 5 * 60           # si aceptas, 5 h más


class Cancelled(Exception):
    """El usuario canceló la acción (no es un error)."""
RED = "#d63a3a"
RED_HOVER = "#b02a2a"
DEFAULT_FOLDER = r"C:\grabaciones" if os.name == "nt" else os.path.join(os.path.expanduser("~"), "grabaciones")


def icon_path() -> str | None:
    """Busca icono.ico (dentro del .exe o en la raíz del proyecto)."""
    for d in (core.app_dir(), os.path.dirname(core.app_dir())):
        p = os.path.join(d, "icono.ico")
        if os.path.isfile(p):
            return p
    return None


def set_icon(win):
    p = icon_path()
    if p and os.name == "nt":
        try:
            win.iconbitmap(p)
        except Exception:  # noqa: BLE001
            pass
    elif p:
        try:  # Linux/macOS: iconphoto con PNG convertido
            from PIL import ImageTk
            win._icon_img = ImageTk.PhotoImage(Image.open(p))
            win.iconphoto(True, win._icon_img)
        except Exception:  # noqa: BLE001
            pass


def add_context_menu(entry, extra=None):
    """Clic derecho en un CTkEntry: Cortar / Copiar / Pegar / Seleccionar todo."""
    w = getattr(entry, "_entry", entry)
    menu = tk.Menu(w, tearoff=0)

    def do(ev):
        w.focus_set()
        w.event_generate(ev)

    def select_all():
        w.focus_set()
        w.select_range(0, "end")
        w.icursor("end")

    if extra:
        for label, fn in extra:
            menu.add_command(label=label, command=fn)
        menu.add_separator()
    menu.add_command(label="Cortar", command=lambda: do("<<Cut>>"))
    menu.add_command(label="Copiar", command=lambda: do("<<Copy>>"))
    menu.add_command(label="Pegar", command=lambda: do("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Seleccionar todo", command=select_all)

    def show(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    w.bind("<Button-3>", show)
    if sys.platform == "darwin":
        w.bind("<Button-2>", show)


def add_text_context_menu(textbox):
    """Clic derecho en el registro: Copiar / Seleccionar todo."""
    w = getattr(textbox, "_textbox", textbox)
    menu = tk.Menu(w, tearoff=0)
    menu.add_command(label="Copiar", command=lambda: w.event_generate("<<Copy>>"))
    menu.add_command(label="Seleccionar todo", command=lambda: w.tag_add("sel", "1.0", "end"))
    w.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))


def open_path(path: str):
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(f"Grabador de Directos {VERSION}")
        self.geometry("1280x800")
        self.minsize(1100, 700)
        set_icon(self)  # icono de la ventana y de la barra de tareas

        self.cfg = core.load_json("config.json", {})
        self.ui_queue: queue.Queue = queue.Queue()
        self.recordings: list[core.Recording] = []
        self.rec_rows: dict[str, dict] = {}
        self.info: core.LinkInfo | None = None
        self.name_edited = False
        self.player = None
        self._thumb_img = None
        self.preview_on = False
        self._limit_dialogs: dict[str, "LimitDialog"] = {}

        self._build_layout()
        self.scheduler = core.Scheduler(self._launch_from_scheduler, log=self.log)
        self.scheduler.start()
        self._refresh_schedules()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._drain_queue)
        self.after(1000, self._tick)
        self.log(f"Grabador de Directos {VERSION} listo. ffmpeg: {core.ffmpeg_path() or 'NO ENCONTRADO'}"
                 f" · vista previa en vivo: {'sí' if MPV_OK else 'no (falta libmpv-2.dll)'}")

    # ------------------------------------------------------------------ UI
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0, minsize=520)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)

        self._build_form(ctk.CTkFrame(self))
        self._build_preview(ctk.CTkFrame(self))
        self._build_tabs()
        self._attach_context_menus(self)
        add_text_context_menu(self.log_box)

    def _build_form(self, f):
        f.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        f.grid_columnconfigure(1, weight=1)
        r = 0
        ctk.CTkLabel(f, text="Enlace", font=ctk.CTkFont(weight="bold")).grid(row=r, column=0, sticky="w", padx=10, pady=(10, 4))
        self.url_var = tk.StringVar(value=self.cfg.get("last_url", ""))
        e = ctk.CTkEntry(f, textvariable=self.url_var, placeholder_text="https://www.youtube.com/watch?v=…")
        self.url_entry = e
        e.grid(row=r, column=1, sticky="ew", padx=4, pady=(10, 4))
        e.bind("<Return>", lambda _e: self.analyze())
        ctk.CTkButton(f, text="Comprobar", width=100, command=self.analyze).grid(row=r, column=2, padx=(4, 10), pady=(10, 4))
        r += 1
        self.badge = ctk.CTkLabel(f, text="", anchor="w", font=ctk.CTkFont(weight="bold"))
        self.badge.grid(row=r, column=1, columnspan=2, sticky="w", padx=4)
        r += 1
        self.title_lbl = ctk.CTkLabel(f, text="Pega un enlace y pulsa Comprobar", anchor="w",
                                      wraplength=380, justify="left", text_color="gray70")
        self.title_lbl.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=(0, 6))
        r += 1

        ctk.CTkLabel(f, text="Carpeta").grid(row=r, column=0, sticky="w", padx=10, pady=4)
        self.folder_var = tk.StringVar(value=self.cfg.get("folder", DEFAULT_FOLDER))
        ctk.CTkEntry(f, textvariable=self.folder_var).grid(row=r, column=1, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(f, text="Examinar…", width=100, command=self._pick_folder).grid(row=r, column=2, padx=(4, 10), pady=4)
        r += 1

        ctk.CTkLabel(f, text="Nombre").grid(row=r, column=0, sticky="w", padx=10, pady=4)
        self.name_var = tk.StringVar(value=self.cfg.get("basename", "Grabacion"))
        ne = ctk.CTkEntry(f, textvariable=self.name_var)
        ne.grid(row=r, column=1, sticky="ew", padx=4, pady=4)
        ne.bind("<Key>", lambda _e: setattr(self, "name_edited", True))
        ctk.CTkLabel(f, text="+ fecha y hora", text_color="gray60").grid(row=r, column=2, sticky="w", padx=(4, 10))
        r += 1

        ctk.CTkLabel(f, text="Calidad").grid(row=r, column=0, sticky="w", padx=10, pady=4)
        self.quality_var = tk.StringVar(value=self.cfg.get("quality", "1080p"))
        ctk.CTkOptionMenu(f, variable=self.quality_var, values=list(core.QUALITIES)).grid(row=r, column=1, sticky="w", padx=4, pady=4)
        r += 1
        ctk.CTkLabel(f, text="Formato").grid(row=r, column=0, sticky="w", padx=10, pady=4)
        self.format_var = tk.StringVar(value=self.cfg.get("fmt", list(core.FORMATS)[0]))
        ctk.CTkOptionMenu(f, variable=self.format_var, values=list(core.FORMATS), width=260).grid(row=r, column=1, sticky="w", padx=4, pady=4)
        r += 1

        ctk.CTkLabel(f, text="Duración").grid(row=r, column=0, sticky="w", padx=10, pady=(10, 4))
        self.mode_var = tk.StringVar(value=self.cfg.get("mode", "Hasta que la pare"))
        ctk.CTkSegmentedButton(f, values=["Hasta que la pare", "Duración fija"], variable=self.mode_var,
                               command=lambda _v: self._update_mode()).grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=(10, 4))
        r += 1
        dur = ctk.CTkFrame(f, fg_color="transparent")
        dur.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        self.hours_var = tk.StringVar(value=str(self.cfg.get("hours", 2)))
        self.mins_var = tk.StringVar(value=str(self.cfg.get("mins", 0)))
        self.hours_e = ctk.CTkEntry(dur, textvariable=self.hours_var, width=50)
        self.hours_e.pack(side="left")
        ctk.CTkLabel(dur, text=" h  ").pack(side="left")
        self.mins_e = ctk.CTkEntry(dur, textvariable=self.mins_var, width=50)
        self.mins_e.pack(side="left")
        ctk.CTkLabel(dur, text=" min").pack(side="left")
        r += 1

        split = ctk.CTkFrame(f, fg_color="transparent")
        split.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=(8, 2))
        self.split_on = tk.BooleanVar(value=self.cfg.get("split_on", False))
        ctk.CTkCheckBox(split, text="Dividir en archivos de", variable=self.split_on,
                        command=self._update_mode).pack(side="left")
        self.split_var = tk.StringVar(value=str(self.cfg.get("split_min", 30)))
        self.split_e = ctk.CTkEntry(split, textvariable=self.split_var, width=50)
        self.split_e.pack(side="left", padx=6)
        ctk.CTkLabel(split, text="min").pack(side="left")
        r += 1

        retry = ctk.CTkFrame(f, fg_color="transparent")
        retry.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=(8, 2))
        ctk.CTkLabel(retry, text="Si no emite, reintentar cada", text_color="gray70").pack(side="left")
        self.retry_int_var = tk.StringVar(value=str(self.cfg.get("retry_interval", 30)))
        ctk.CTkEntry(retry, textvariable=self.retry_int_var, width=42).pack(side="left", padx=4)
        ctk.CTkLabel(retry, text="s, máx.", text_color="gray70").pack(side="left")
        self.retry_max_var = tk.StringVar(value=str(self.cfg.get("retry_max", 60)))
        ctk.CTkEntry(retry, textvariable=self.retry_max_var, width=42).pack(side="left", padx=4)
        ctk.CTkLabel(retry, text="veces (0 = siempre)", text_color="gray70").pack(side="left")
        r += 1

        btns = ctk.CTkFrame(f, fg_color="transparent")
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=(16, 12))
        ctk.CTkButton(btns, text="●  Grabar ahora", fg_color=RED, hover_color=RED_HOVER, height=40,
                      font=ctk.CTkFont(size=15, weight="bold"), command=self.record_now).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(btns, text="🕒  Programar…", height=40, font=ctk.CTkFont(size=15),
                      command=self.open_schedule_dialog).pack(side="left", expand=True, fill="x", padx=(6, 0))
        self._update_mode()

    def _build_preview(self, f):
        f.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(f, text="Vista previa", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        self.preview_box = tk.Frame(f, bg="black", height=360)
        self.preview_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        self.video_frame = tk.Frame(self.preview_box, bg="black")
        self.video_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.thumb_lbl = tk.Label(self.preview_box, bg="black", fg="gray60", text="Sin vista previa",
                                  font=("Segoe UI", 12))
        self.thumb_lbl.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.preview_box.bind("<Configure>", lambda _e: self._fit_thumb())

        ctl = ctk.CTkFrame(f, fg_color="transparent")
        ctl.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))
        self.mute_var = tk.BooleanVar(value=True)
        ctk.CTkSwitch(ctl, text="Silenciar", variable=self.mute_var, command=self._apply_audio).pack(side="left")
        ctk.CTkLabel(ctl, text="  Volumen").pack(side="left")
        self.vol = ctk.CTkSlider(ctl, from_=0, to=100, width=140, command=lambda _v: self._apply_audio())
        self.vol.set(60)
        self.vol.pack(side="left", padx=6)
        self.preview_btn = ctk.CTkButton(ctl, text="■  Parar vista previa", width=170, fg_color="gray30",
                                         command=self.toggle_preview, state="disabled")
        self.preview_btn.pack(side="right")

    def _build_tabs(self):
        tabs = ctk.CTkTabview(self, height=260)
        tabs.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=12, pady=(0, 12))
        t_act = tabs.add("Grabaciones en curso")
        t_sch = tabs.add("Programadas")
        t_log = tabs.add("Registro")

        t_act.grid_columnconfigure(0, weight=1)
        t_act.grid_rowconfigure(0, weight=1)
        self.act_list = ctk.CTkScrollableFrame(t_act)
        self.act_list.grid(row=0, column=0, sticky="nsew")
        self.act_list.grid_columnconfigure(0, weight=1)
        self.act_empty = ctk.CTkLabel(self.act_list, text="No hay grabaciones en curso.", text_color="gray60")
        self.act_empty.grid(row=0, column=0, pady=20)
        bar = ctk.CTkFrame(t_act, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ctk.CTkButton(bar, text="Quitar terminadas", width=140, fg_color="gray30",
                      command=self._clear_finished).pack(side="right")

        t_sch.grid_columnconfigure(0, weight=1)
        t_sch.grid_rowconfigure(0, weight=1)
        self.sch_list = ctk.CTkScrollableFrame(t_sch)
        self.sch_list.grid(row=0, column=0, sticky="nsew")
        self.sch_list.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t_sch, text="Las programaciones se ejecutan mientras esta aplicación esté abierta "
                                 "(puedes minimizarla).", text_color="gray60").grid(row=1, column=0, sticky="w", pady=(4, 0))

        t_log.grid_columnconfigure(0, weight=1)
        t_log.grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(t_log, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

    def _attach_context_menus(self, parent):
        for w in parent.winfo_children():
            if isinstance(w, ctk.CTkEntry):
                if w is getattr(self, "url_entry", None):
                    add_context_menu(w, extra=[("Pegar y comprobar", self._paste_and_check)])
                else:
                    add_context_menu(w)
            self._attach_context_menus(w)

    def _paste_and_check(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_var.set(text)
        self.analyze()

    # ----------------------------------------------------------- helpers
    def ui(self, fn, *args):
        """Ejecuta fn en el hilo de la interfaz."""
        self.ui_queue.put((fn, args))

    def _drain_queue(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:  # noqa: BLE001
                    print("UI error:", e)
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)

    def log(self, msg: str):
        if threading.current_thread() is not threading.main_thread():
            self.ui(self.log, msg)
            return
        if not msg.startswith("["):
            msg = f"[{datetime.now():%H:%M:%S}] {msg}"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 3000:
            self.log_box.delete("1.0", f"{lines - 3000}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or None)
        if d:
            self.folder_var.set(os.path.normpath(d))

    def _update_mode(self):
        fixed = self.mode_var.get() == "Duración fija"
        for w in (self.hours_e, self.mins_e):
            w.configure(state="normal" if fixed else "disabled")
        self.split_e.configure(state="normal" if self.split_on.get() else "disabled")

    def _int(self, var, name, minimum=0):
        try:
            v = int(float(var.get().strip().replace(",", ".")))
            if v < minimum:
                raise ValueError
            return v
        except ValueError:
            raise ValueError(f"«{name}» debe ser un número ≥ {minimum}")

    def _collect(self) -> core.JobConfig:
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://", "hls://")):
            raise ValueError("Pega un enlace válido (http/https).")
        folder = self.folder_var.get().strip()
        if not folder:
            raise ValueError("Elige una carpeta de destino.")
        folder = os.path.normpath(os.path.expandvars(os.path.expanduser(folder)))
        if not os.path.isdir(folder):
            if os.path.exists(folder):
                raise ValueError(f"«{folder}» es un archivo, no una carpeta.")
            if not messagebox.askyesno("Carpeta no encontrada",
                                       f"La carpeta\n\n{folder}\n\nno existe. ¿Quieres crearla?"):
                raise Cancelled()
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                raise ValueError(f"No se pudo crear la carpeta:\n{folder}\n\n{e.strerror or e}")
            self.log(f"Carpeta creada: {folder}")
            self.folder_var.set(folder)
        duration = 0
        if self.mode_var.get() == "Duración fija":
            duration = self._int(self.hours_var, "horas") * 60 + self._int(self.mins_var, "minutos")
            if duration <= 0:
                raise ValueError("La duración debe ser mayor que 0.")
        split = self._int(self.split_var, "dividir", 1) if self.split_on.get() else 0
        cfg = core.JobConfig(
            url=url, folder=folder, basename=core.safe_name(self.name_var.get() or "Grabacion"),
            quality=self.quality_var.get(), fmt=self.format_var.get(), duration_min=duration,
            split_min=split, retry_interval=self._int(self.retry_int_var, "reintentar cada", 5),
            retry_max=self._int(self.retry_max_var, "máx. reintentos", 0),
        )
        self._save_cfg()
        return cfg

    def _save_cfg(self):
        self.cfg.update({
            "last_url": self.url_var.get().strip(), "folder": self.folder_var.get().strip(),
            "basename": self.name_var.get(), "quality": self.quality_var.get(),
            "fmt": self.format_var.get(), "mode": self.mode_var.get(),
            "hours": self.hours_var.get(), "mins": self.mins_var.get(),
            "split_on": self.split_on.get(), "split_min": self.split_var.get(),
            "retry_interval": self.retry_int_var.get(), "retry_max": self.retry_max_var.get(),
        })
        try:
            core.save_json("config.json", self.cfg)
        except OSError:
            pass

    # ----------------------------------------------------------- análisis
    def analyze(self, then=None):
        url = self.url_var.get().strip()
        if not url:
            return
        self.badge.configure(text="Comprobando…", text_color="gray70")
        self.title_lbl.configure(text=url)

        def work():
            info = core.analyze(url)
            thumb = core.download_bytes(info.thumbnail) if info.thumbnail else None
            self.ui(self._show_info, info, thumb, then)

        threading.Thread(target=work, daemon=True).start()

    def _show_info(self, info: core.LinkInfo, thumb: bytes | None, then=None):
        self.info = info
        labels = {
            "directo": ("●  EN DIRECTO", RED),
            "proximo": ("◷  PRÓXIMAMENTE (se esperará a que empiece)", "#e0a030"),
            "video": ("▶  VÍDEO (se descargará con yt-dlp)", "#4a9eff"),
            "desconocido": ("✕  No disponible", "gray60"),
        }
        text, color = labels[info.kind]
        self.badge.configure(text=text, text_color=color)
        title = info.title or info.error or info.url
        if info.channel:
            title = f"{title}\n{info.channel}"
        self.title_lbl.configure(text=title[:300])
        if info.title and not self.name_edited:
            self.name_var.set(core.safe_name(info.title, 60))
        self.log(f"{text.strip('●◷▶✕ ')}: {info.title or info.url}")
        if info.error and info.kind == "desconocido":
            self.log(f"Detalle: {info.error}")

        self._set_thumb(thumb)
        if info.kind == "directo" and info.preview_url and MPV_OK:
            self.start_preview(info.preview_url)
        else:
            self.stop_preview(keep_thumb=True)
        if info.kind == "directo" and not MPV_OK:
            self.log("Vista previa en vivo no disponible (falta libmpv-2.dll).")
        if then:
            then()

    # ----------------------------------------------------------- vista previa
    def _set_thumb(self, data: bytes | None):
        self._thumb_src = None
        if data:
            try:
                self._thumb_src = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:  # noqa: BLE001
                self._thumb_src = None
        self._fit_thumb()

    def _fit_thumb(self):
        src = getattr(self, "_thumb_src", None)
        if not src:
            self.thumb_lbl.configure(image="", text="Sin vista previa")
            return
        from PIL import ImageTk
        w = max(50, self.preview_box.winfo_width())
        h = max(50, self.preview_box.winfo_height())
        img = src.copy()
        img.thumbnail((w, h))
        self._thumb_img = ImageTk.PhotoImage(img)
        self.thumb_lbl.configure(image=self._thumb_img, text="")

    def start_preview(self, url: str):
        try:
            if self.player is None:
                self.player = mpv.MPV(
                    wid=str(int(self.video_frame.winfo_id())), ytdl=False, osc=False,
                    input_default_bindings=False, input_vo_keyboard=False, keep_open="no",
                    hwdec="auto-safe", cache="yes", demuxer_max_bytes="30MiB",
                    loglevel="error",
                    **({"vo": os.environ["GRABADOR_VO"]} if os.environ.get("GRABADOR_VO") else {}),
                )
            self._apply_audio()
            self.player.play(url)
            self.thumb_lbl.lower(self.video_frame)
            self.preview_on = True
        except Exception as e:  # noqa: BLE001
            self.log(f"No se pudo iniciar la vista previa: {e}")
            self.thumb_lbl.lift()
            self.preview_on = False
        self._update_preview_btn()

    def stop_preview(self, keep_thumb: bool = True):
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass
        self.thumb_lbl.lift()
        self.preview_on = False
        if not keep_thumb:
            self._set_thumb(None)
        self._update_preview_btn()

    def _update_preview_btn(self):
        can = MPV_OK and self.info is not None and self.info.kind == "directo"
        if self.preview_on:
            self.preview_btn.configure(text="■  Parar vista previa", state="normal")
        else:
            self.preview_btn.configure(text="▶  Reanudar vista previa", state="normal" if can else "disabled")

    def toggle_preview(self):
        if self.preview_on:
            self.stop_preview()
            return
        if not (MPV_OK and self.info and self.info.kind == "directo"):
            return
        self.preview_btn.configure(text="Cargando…", state="disabled")
        url = self.info.url

        def work():
            # Pedimos una URL nueva: las de YouTube caducan al cabo de unas horas.
            try:
                fresh = core.preview_url_for(url)
            except Exception as e:  # noqa: BLE001
                fresh = None
                self.log(f"Vista previa: {e}")
            if fresh:
                self.ui(self.start_preview, fresh)
            else:
                self.log("El directo no está emitiendo ahora mismo.")
                self.ui(self._update_preview_btn)

        threading.Thread(target=work, daemon=True).start()

    def _apply_audio(self):
        if self.player is not None:
            try:
                self.player.mute = bool(self.mute_var.get())
                self.player.volume = int(self.vol.get())
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------- grabar
    def record_now(self):
        try:
            cfg = self._collect()
        except Cancelled:
            return
        except ValueError as e:
            messagebox.showwarning("Revisa los datos", str(e))
            return
        if self.info is None or self.info.url != cfg.url:
            self.analyze(then=self.record_now)
            return
        if self.info.kind == "desconocido":
            if not messagebox.askyesno("Sin emisión", "No se detecta emisión en ese enlace.\n\n"
                                       "¿Grabar igualmente y esperar a que empiece (con los reintentos configurados)?"):
                return
        cfg.kind = "video" if self.info.kind == "video" else "directo"
        if cfg.duration_min == 0 and cfg.kind == "directo":
            cfg.auto_limit_min = AUTO_LIMIT_MIN
        self._start_recording(cfg)

    def _launch_from_scheduler(self, cfg: core.JobConfig):
        self.ui(self._start_recording, cfg)

    def _start_recording(self, cfg: core.JobConfig):
        rec = core.Recording(cfg, log=self.log)
        self.recordings.append(rec)
        rec.start()
        self._add_rec_row(rec)
        if cfg.duration_min:
            dur = f"{cfg.duration_min} min"
        elif cfg.end_at:
            dur = "hasta fin programado"
        elif cfg.auto_limit_min:
            dur = f"hasta que la pares (máx. {cfg.auto_limit_min // 60} h, ampliable)"
        else:
            dur = "hasta que la pares"
        self.log(f"Inicio [{cfg.origin}] «{cfg.basename}» · {cfg.quality} · {cfg.fmt} · {dur}"
                 + (f" · cortes cada {cfg.split_min} min" if cfg.split_min else ""))

    def _add_rec_row(self, rec: core.Recording):
        self.act_empty.grid_forget()
        row = ctk.CTkFrame(self.act_list)
        row.grid(row=len(self.rec_rows) + 1, column=0, sticky="ew", pady=3, padx=2)
        row.grid_columnconfigure(1, weight=1)
        dot = ctk.CTkLabel(row, text="●", text_color=RED, width=20)
        dot.grid(row=0, column=0, rowspan=2, padx=(8, 4))
        name = ctk.CTkLabel(row, text=f"{rec.cfg.basename}  ·  {rec.cfg.quality} · {rec.cfg.fmt.split(' ')[0]}"
                            + ("  ·  programada" if rec.cfg.origin == "programada" else ""),
                            anchor="w", font=ctk.CTkFont(weight="bold"))
        name.grid(row=0, column=1, sticky="w")
        det = ctk.CTkLabel(row, text="", anchor="w", text_color="gray70")
        det.grid(row=1, column=1, sticky="w")
        ctk.CTkButton(row, text="Abrir carpeta", width=110, fg_color="gray30",
                      command=lambda: open_path(rec.cfg.folder)).grid(row=0, column=2, rowspan=2, padx=4, pady=6)
        stop_btn = ctk.CTkButton(row, text="■ Detener", width=100, fg_color=RED, hover_color=RED_HOVER,
                                 command=lambda: self._stop_rec(rec))
        stop_btn.grid(row=0, column=3, rowspan=2, padx=(4, 8), pady=6)
        self.rec_rows[rec.id] = {"frame": row, "dot": dot, "det": det, "stop": stop_btn}

    def _stop_rec(self, rec: core.Recording):
        if not rec.finished:
            rec.stop()
            rec.status = "Deteniendo…"
            self.log(f"Deteniendo «{rec.cfg.basename}»…")

    def _clear_finished(self):
        for rec in [r for r in self.recordings if r.finished]:
            row = self.rec_rows.pop(rec.id, None)
            if row:
                row["frame"].destroy()
            self.recordings.remove(rec)
        if not self.recordings:
            self.act_empty.grid(row=0, column=0, pady=20)

    def _tick(self):
        now = time.time()
        for rec in self.recordings:
            row = self.rec_rows.get(rec.id)
            if not row:
                continue
            parts = [rec.status, f"transcurrido {core.fmt_duration(now - rec.started_at)}"]
            rem = rec.remaining()
            if rem is not None and not rec.finished:
                parts.append(f"{'tope' if rec.extendable else 'quedan'} {core.fmt_duration(rem)}")
                if rec.extendable and not rec.limit_warned and rem <= WARN_BEFORE_MIN * 60:
                    rec.limit_warned = True
                    self._ask_extend(rec)
            parts.append(core.fmt_bytes(rec.recorded_bytes))
            parts.append(f"{len(rec.files)} archivo(s)")
            if rec.error:
                parts.append(rec.error[:120])
            row["det"].configure(text="  ·  ".join(parts))
            if rec.finished:
                row["dot"].configure(text_color="#3fb950" if not rec.error else "#e0a030")
                row["stop"].configure(state="disabled", fg_color="gray25")
            elif rec.status.startswith("Grabando") or rec.status.startswith("Descargando"):
                # parpadeo del punto rojo
                row["dot"].configure(text_color=RED if int(now) % 2 == 0 else "#5a1a1a")
            else:
                row["dot"].configure(text_color="#e0a030")
        if int(now) % 30 == 0:
            self._refresh_schedules()
        self.after(1000, self._tick)

    def _ask_extend(self, rec: core.Recording):
        old = self._limit_dialogs.pop(rec.id, None)
        if old is not None and old.winfo_exists():
            old.destroy()
        self._limit_dialogs[rec.id] = LimitDialog(self, rec)

    # ----------------------------------------------------------- programación
    def open_schedule_dialog(self):
        try:
            cfg = self._collect()
        except Cancelled:
            return
        except ValueError as e:
            messagebox.showwarning("Revisa los datos", str(e))
            return
        ScheduleDialog(self, cfg)

    def add_schedule(self, s: core.Schedule):
        self.scheduler.add(s)
        nxt = s.next_run()
        self.log(f"Programada «{s.basename}»: {s.describe()}"
                 + (f" · próxima {nxt:%d/%m %H:%M}" if nxt else ""))
        self._refresh_schedules()

    def _refresh_schedules(self):
        for w in self.sch_list.winfo_children():
            w.destroy()
        items = list(self.scheduler.items)
        if not items:
            ctk.CTkLabel(self.sch_list, text="No hay grabaciones programadas.", text_color="gray60").grid(row=0, column=0, columnspan=4, pady=20)
            return
        for i, s in enumerate(items):
            var = tk.BooleanVar(value=s.enabled)
            ctk.CTkSwitch(self.sch_list, text="", variable=var, width=40,
                          command=lambda sid=s.id: (self.scheduler.toggle(sid), self._refresh_schedules())
                          ).grid(row=i, column=0, padx=(6, 2), pady=4)
            nxt = s.next_run() if s.enabled else None
            nxt_txt = f"próxima: {core.DAY_NAMES[nxt.weekday()]} {nxt:%d/%m %H:%M}" if nxt else ("desactivada" if not s.enabled else "finalizada")
            ctk.CTkLabel(self.sch_list, anchor="w", justify="left",
                         text=f"{s.basename}   ·   {s.describe()}   ·   {s.quality} · {s.fmt.split(' ')[0]}\n"
                              f"{s.url}   ·   {nxt_txt}",
                         ).grid(row=i, column=1, sticky="w", padx=6, pady=4)
            ctk.CTkButton(self.sch_list, text="Cargar", width=70, fg_color="gray30",
                          command=lambda s=s: self._load_schedule(s)).grid(row=i, column=2, padx=4)
            ctk.CTkButton(self.sch_list, text="Eliminar", width=80, fg_color="gray30", hover_color=RED_HOVER,
                          command=lambda sid=s.id: self._delete_schedule(sid)).grid(row=i, column=3, padx=(4, 8))

    def _load_schedule(self, s: core.Schedule):
        self.url_var.set(s.url)
        self.folder_var.set(s.folder)
        self.name_var.set(s.basename)
        self.name_edited = True
        self.quality_var.set(s.quality)
        self.format_var.set(s.fmt)

    def _delete_schedule(self, sid: str):
        if messagebox.askyesno("Eliminar", "¿Eliminar esta programación?"):
            self.scheduler.remove(sid)
            self._refresh_schedules()

    # ----------------------------------------------------------- cierre
    def _on_close(self):
        active = [r for r in self.recordings if not r.finished]
        pend = [s for s in self.scheduler.items if s.enabled and s.next_run()]
        if active or pend:
            msg = []
            if active:
                msg.append(f"Hay {len(active)} grabación(es) en curso: se detendrán y se cerrarán los archivos.")
            if pend:
                msg.append(f"Hay {len(pend)} programación(es) pendientes que no se ejecutarán con la app cerrada.")
            if not messagebox.askyesno("¿Salir?", "\n\n".join(msg) + "\n\n¿Salir igualmente?"):
                return
        self._save_cfg()
        self.scheduler.stop()
        for r in active:
            r.stop()
        deadline = time.time() + 15
        for r in active:
            r.join(timeout=max(0.1, deadline - time.time()))
        try:
            if self.player is not None:
                self.player.terminate()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


class LimitDialog(ctk.CTkToplevel):
    """Aviso a las 4 h 50: ¿seguir grabando 5 h más? Por defecto: No."""

    def __init__(self, app: App, rec: core.Recording):
        super().__init__(app)
        self.app, self.rec = app, rec
        self.title("¿Seguir grabando?")
        try:  # centrado sobre la ventana principal
            x = app.winfo_rootx() + app.winfo_width() // 2 - 230
            y = app.winfo_rooty() + app.winfo_height() // 2 - 110
            self.geometry(f"460x220+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            self.geometry("460x220")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.after(250, lambda: set_icon(self))
        horas = EXTEND_MIN // 60
        ctk.CTkLabel(self, text=f"«{rec.cfg.basename}»", font=ctk.CTkFont(size=15, weight="bold"),
                     wraplength=420).pack(padx=20, pady=(18, 4))
        self.msg = ctk.CTkLabel(self, text="", justify="center")
        self.msg.pack(padx=20, pady=4)
        b = ctk.CTkFrame(self, fg_color="transparent")
        b.pack(pady=16)
        self.no_btn = ctk.CTkButton(b, text="No, terminar", width=150, fg_color=RED, hover_color=RED_HOVER,
                                    border_width=2, border_color="white", command=self._no)
        self.no_btn.pack(side="left", padx=8)
        ctk.CTkButton(b, text=f"Sí, {horas} horas más", width=150, fg_color="gray30",
                      command=self._yes).pack(side="left", padx=8)
        # "No" es la opción por defecto: Enter o Escape = No
        self.bind("<Return>", lambda _e: self._no())
        self.bind("<Escape>", lambda _e: self._no())
        self.protocol("WM_DELETE_WINDOW", self._no)
        try:
            app.deiconify()
            self.lift()
            self.focus_force()
            self.bell()
        except tk.TclError:
            pass
        self._update()

    def _update(self):
        if not self.winfo_exists():
            return
        if self.rec.finished:
            self.destroy()
            return
        rem = self.rec.remaining() or 0
        self.msg.configure(text=f"La grabación lleva {core.fmt_duration(time.time() - self.rec.started_at)}.\n"
                                f"Se detendrá automáticamente en {core.fmt_duration(rem)}.\n\n"
                                f"¿Quieres que siga grabando {EXTEND_MIN // 60} horas más?")
        self.after(1000, self._update)

    def _yes(self):
        self.rec.extend(EXTEND_MIN)
        self.destroy()

    def _no(self):
        self.app.log(f"«{self.rec.cfg.basename}»: no se amplía; terminará al llegar al tope.")
        self.destroy()


class ScheduleDialog(ctk.CTkToplevel):
    def __init__(self, app: App, cfg: core.JobConfig):
        super().__init__(app)
        self.app, self.cfg = app, cfg
        self.title("Programar grabación")
        self.geometry("470x430")
        self.resizable(False, False)
        self.transient(app)
        self.after(100, self.grab_set)
        self.after(250, lambda: set_icon(self))  # customtkinter pone el suyo a los 200 ms
        self.grid_columnconfigure(1, weight=1)

        start = datetime.now() + timedelta(minutes=5)
        r = 0
        ctk.CTkLabel(self, text=cfg.basename, font=ctk.CTkFont(size=15, weight="bold"), wraplength=420
                     ).grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 2))
        r += 1
        ctk.CTkLabel(self, text=cfg.url, text_color="gray60", wraplength=420
                     ).grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 10))
        r += 1
        ctk.CTkLabel(self, text="Repetición").grid(row=r, column=0, sticky="w", padx=16, pady=4)
        self.repeat = tk.StringVar(value="Una vez")
        ctk.CTkOptionMenu(self, variable=self.repeat, values=core.REPEATS,
                          command=lambda _v: self._upd()).grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        self.date_lbl = ctk.CTkLabel(self, text="Fecha (dd/mm/aaaa)")
        self.date_lbl.grid(row=r, column=0, sticky="w", padx=16, pady=4)
        self.date = tk.StringVar(value=start.strftime("%d/%m/%Y"))
        self.date_e = ctk.CTkEntry(self, textvariable=self.date, width=120)
        self.date_e.grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        ctk.CTkLabel(self, text="Hora de inicio (HH:MM)").grid(row=r, column=0, sticky="w", padx=16, pady=4)
        self.hour = tk.StringVar(value=start.strftime("%H:%M"))
        ctk.CTkEntry(self, textvariable=self.hour, width=120).grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        self.days_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.days_frame.grid(row=r, column=0, columnspan=2, sticky="w", padx=12, pady=4)
        self.day_vars = []
        for i, d in enumerate(core.DAY_NAMES):
            v = tk.BooleanVar(value=i < 5)
            ctk.CTkCheckBox(self.days_frame, text=d, variable=v, width=50).pack(side="left", padx=2)
            self.day_vars.append(v)
        r += 1
        dur = f"{cfg.duration_min // 60} h {cfg.duration_min % 60} min" if cfg.duration_min else \
            "hasta que termine el directo (sin límite)"
        split = f"\nArchivos de {cfg.split_min} min" if cfg.split_min else ""
        ctk.CTkLabel(self, text=f"Duración: {dur}{split}\nCalidad: {cfg.quality} · Formato: {cfg.fmt}",
                     justify="left", text_color="gray70").grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=(10, 4))
        r += 1
        ctk.CTkLabel(self, text="Consejo: para un programa que se emite cada día en YouTube usa el enlace\n"
                                "del canal terminado en /live (ej. youtube.com/@canal/live), porque\n"
                                "cada directo nuevo tiene un enlace distinto.",
                     justify="left", text_color="gray55", font=ctk.CTkFont(size=11)
                     ).grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=4)
        r += 1
        b = ctk.CTkFrame(self, fg_color="transparent")
        b.grid(row=r, column=0, columnspan=2, sticky="ew", padx=16, pady=14)
        ctk.CTkButton(b, text="Cancelar", fg_color="gray30", command=self.destroy).pack(side="right", padx=4)
        ctk.CTkButton(b, text="Guardar programación", command=self._save).pack(side="right", padx=4)
        self._upd()
        app._attach_context_menus(self)

    def _upd(self):
        once = self.repeat.get() == "Una vez"
        self.date_e.configure(state="normal" if once else "disabled")
        if self.repeat.get() == "Días concretos":
            self.days_frame.grid()
        else:
            self.days_frame.grid_remove()

    def _save(self):
        try:
            hh = datetime.strptime(self.hour.get().strip(), "%H:%M")
            if self.repeat.get() == "Una vez":
                d = datetime.strptime(self.date.get().strip(), "%d/%m/%Y")
                start = d.replace(hour=hh.hour, minute=hh.minute)
                if start + timedelta(minutes=self.cfg.duration_min or 10) < datetime.now():
                    raise ValueError("Esa fecha y hora ya han pasado.")
            else:
                # válida desde hoy (si la hora de hoy ya pasó, empieza mañana)
                start = datetime.combine(datetime.now().date(), hh.time())
            days = [i for i, v in enumerate(self.day_vars) if v.get()]
            if self.repeat.get() == "Días concretos" and not days:
                raise ValueError("Marca al menos un día.")
        except ValueError as e:
            msg = str(e) if "pasado" in str(e) or "día" in str(e) else "Revisa la fecha (dd/mm/aaaa) y la hora (HH:MM)."
            messagebox.showwarning("Programar", msg, parent=self)
            return
        c = self.cfg
        s = core.Schedule(url=c.url, folder=c.folder, basename=c.basename, quality=c.quality, fmt=c.fmt,
                          start=start.strftime("%Y-%m-%d %H:%M"), duration_min=c.duration_min,
                          split_min=c.split_min, repeat=self.repeat.get(), days=days,
                          retry_interval=c.retry_interval, retry_max=c.retry_max)
        self.app.add_schedule(s)
        self.destroy()


def main():
    if os.name == "nt":
        try:  # para que la barra de tareas use nuestro icono y no el de Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("elunele.GrabadorDirectos")
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()  # customtkinter ya gestiona el escalado DPI de Windows


if __name__ == "__main__":
    main()
