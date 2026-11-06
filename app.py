"""
Audio Transcriber - window version
==================================

A small control window around the transcription engine in transcriber.py:
live status and progress, easy switches for every setting, recent
transcripts, and a tray icon so it can keep running in the background.

Run with --console for the original text-only mode (e.g. on a server),
or --minimized to start hidden in the tray (used by "Start with Windows").
"""

import logging
import os
import queue
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import transcriber as T

log = logging.getLogger("transcriber.app")

# Friendly labels for the settings -> the value stored in config.json.
MODEL_CHOICES = {
    "Tiny – very fast, rough": "tiny",
    "Base – fast, basic": "base",
    "Small – fast, good": "small",
    "Medium – best balance (recommended)": "medium",
    "Large – most accurate, slowest": "large-v3",
}
LANGUAGE_CHOICES = {
    "Auto-detect": "auto",
    "English": "en",
    "Filipino / Tagalog": "tl",
}
ORDER_CHOICES = {
    "Newest first": "newest_first",
    "Oldest first": "oldest_first",
    "By name (A–Z)": "name",
}
MODE_CHOICES = {"Fast": "fast", "Accurate": "accurate"}

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AudioTranscriber"


# ---------------------------------------------------------------------------
# Engine: runs transcriber.py's loop in a background thread
# ---------------------------------------------------------------------------

class Engine(T.Hooks):
    """Runs the transcription loop in a background thread and keeps a
    snapshot of what it's doing for the window to show."""

    def __init__(self, locked_folder=None):
        # The Inbox this process already holds the single-instance lock for.
        self.locked_folder = locked_folder
        self.lock = threading.Lock()
        self.state = {
            "phase": "starting",   # starting, loading, working, idle, paused, error
            "message": "Starting...",
            "file": None,
            "percent": 0.0,
            "eta": None,           # seconds left, or None
            "waiting": 0,
            "error": None,
        }
        self.events = queue.Queue()  # ("done"/"failed", data) for notifications
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._restart = threading.Event()
        self._wake = threading.Event()
        self._skip = threading.Event()
        self.skipped = {}  # files skipped/failed this session (kept across restarts)
        self._file_started = 0.0
        self._paused_for = 0.0
        self.thread = threading.Thread(target=self._run, name="engine", daemon=True)

    # --- control (called from the window) ---------------------------------

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._paused.clear()
        self._wake.set()

    def skip_current(self):
        """Abandon the file being transcribed and move on to the next one."""
        self._skip.set()
        self._paused.clear()  # a paused file can be skipped too
        self._update(phase="working", message="Skipping...")

    def set_paused(self, paused: bool):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
            self._wake.set()
        self._update(phase="paused" if paused else self._phase_before_pause())

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def settings_changed(self):
        """Pick up config.json again (after the current file, if any)."""
        self._restart.set()
        self._wake.set()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    # --- Hooks (called from the engine thread) ----------------------------

    def checkpoint(self):
        if self._stop.is_set():
            raise T.Stopped()
        if self._skip.is_set():
            self._skip.clear()
            raise T.Skipped()
        if self._paused.is_set():
            began = time.time()
            while self._paused.is_set() and not self._stop.is_set():
                time.sleep(0.2)
            self._paused_for += time.time() - began
            if self._stop.is_set():
                raise T.Stopped()

    def sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()
        if self._stop.is_set():
            raise T.Stopped()

    def should_restart(self):
        return self._restart.is_set() or self._stop.is_set()

    def progress(self, audio, percent):
        worked = time.time() - self._file_started - self._paused_for
        eta = worked * (100 - percent) / percent if percent >= 2 else None
        self._update(percent=percent, eta=eta)

    def event(self, kind, **data):
        if kind == "started":
            self._skip.clear()  # a late click must not skip the next file
            self._file_started = time.time()
            self._paused_for = 0.0
            self._update(phase="working", file=data["audio"].name, percent=0.0,
                         eta=None, waiting=data["waiting"],
                         message=f"Transcribing {data['audio'].name}")
        elif kind in ("done", "failed", "skipped"):
            self._update(file=None, percent=0.0, eta=None)
            self.events.put((kind, data))
        elif kind == "queue":
            self._update(waiting=data["waiting"])
            if self.state["phase"] in ("loading", "starting"):
                self._update(phase="idle", message="Checking the Inbox...")
        elif kind == "idle":
            self._update(phase="idle", message="All caught up – waiting for new files")

    # --- internals --------------------------------------------------------

    def _phase_before_pause(self):
        return "working" if self.state["file"] else "idle"

    def _update(self, **changes):
        with self.lock:
            if self._paused.is_set() and changes.get("phase") in ("working", "idle"):
                changes["phase"] = "paused"
            self.state.update(changes)

    def _run(self):
        model, model_key, locked_folder = None, None, self.locked_folder
        while not self._stop.is_set():
            self._restart.clear()
            try:
                config = T.load_config()
                input_dir = Path(config["input_folder"])
                output_dir = Path(config["output_folder"])
                for folder in (input_dir, output_dir):
                    folder.mkdir(parents=True, exist_ok=True)
                if input_dir != locked_folder:
                    T.ensure_single_instance(input_dir)
                    locked_folder = input_dir

                key = (config["model_size"], config["device"])
                if key != model_key:
                    model = None  # free the old model's memory first
                    self._update(phase="loading", error=None,
                                 message=f"Loading the {config['model_size']} model "
                                         "(the first time downloads it)...")
                    model = T.load_model(config)
                    model_key = key

                self._update(phase="idle", error=None, message="Checking the Inbox...")
                T.Transcriber(model, config, self, failed=self.skipped).run_forever()
            except T.Stopped:
                break
            except Exception as e:  # a UserError or anything unexpected
                if not isinstance(e, T.UserError):
                    T.file_only.error("Engine error:\n%s", T.traceback.format_exc())
                log.error("%s", e)
                self._update(phase="error", error=str(e), file=None,
                             message="Stopped – see the message below")
                # Wait for a settings change (e.g. a fixed folder), then retry.
                while not (self._restart.is_set() or self._stop.is_set()):
                    self._wake.wait(1)
                    self._wake.clear()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def make_icon_image(size=64):
    """The app icon: white sound-wave bars on a blue rounded square."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(31, 106, 165))
    heights = (0.30, 0.55, 0.80, 0.55, 0.30)
    bar_w = size / 11
    for i, h in enumerate(heights):
        x = size * (0.5 + (i - 2) * 0.16) - bar_w / 2
        half = size * h / 2
        d.rounded_rectangle((x, size / 2 - half, x + bar_w, size / 2 + half),
                            radius=bar_w / 2, fill="white")
    return img


def save_ico(path):
    """Write the icon as a .ico file (used by build.bat for the .exe)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    make_icon_image(256).save(path, sizes=[(16, 16), (24, 24), (32, 32),
                                           (48, 48), (64, 64), (256, 256)])


def open_path(path):
    try:
        os.startfile(str(path))
    except OSError as e:
        log.error("Could not open %s: %s", path, e)


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "less than a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def format_when(timestamp):
    ago = time.time() - timestamp
    if ago < 60:
        return "just now"
    if ago < 3600:
        return f"{int(ago // 60)} min ago"
    when = datetime.fromtimestamp(timestamp)
    if when.date() == datetime.now().date():
        return when.strftime("today %H:%M")
    return when.strftime("%b %d, %H:%M")


def label_for(choices, value, fallback_prefix="Other"):
    for label, v in choices.items():
        if v == value:
            return label
    return f"{fallback_prefix} ({value})"


def exe_command():
    """Command line for "Start with Windows", or None when running from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return None


def get_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, exe_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


class QueueLogHandler(logging.Handler):
    """Sends log lines to the window's Activity box."""

    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def build_app_class():
    """Create the window class (drag-and-drop support if tkinterdnd2 loads)."""
    import customtkinter as ctk

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        base = (ctk.CTk, TkinterDnD.DnDWrapper)
    except Exception:
        TkinterDnD, DND_FILES = None, None
        base = (ctk.CTk,)

    class App(*base):
        def __init__(self, engine, start_hidden=False):
            super().__init__()
            self.dnd = False
            if TkinterDnD is not None:
                try:
                    self.TkdndVersion = TkinterDnD._require(self)
                    self.dnd = True
                except Exception:
                    pass

            self.engine = engine
            self.ui_calls = queue.Queue()   # work handed over from other threads
            self.log_lines = queue.Queue()
            self.tray = None
            self.told_about_tray = False
            self.quitting = False
            self.recent_key = None

            handler = QueueLogHandler(self.log_lines)
            handler.addFilter(T.setup_logging.only_ours)
            logging.getLogger().addHandler(handler)

            self.title(f"{T.APP_NAME} {T.__version__}")
            self.geometry("940x640")
            self.minsize(860, 600)
            try:
                from PIL import ImageTk
                self._icon = ImageTk.PhotoImage(make_icon_image(64))
                self.iconphoto(True, self._icon)
            except Exception:
                pass
            self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

            self.grid_columnconfigure(0, weight=3, uniform="col")
            self.grid_columnconfigure(1, weight=2, uniform="col")
            self.grid_rowconfigure(0, weight=1)
            left = ctk.CTkFrame(self, fg_color="transparent")
            left.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)
            right = ctk.CTkFrame(self, fg_color="transparent")
            right.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=16)
            self._build_left(left)
            self._build_right(right)

            self.load_settings_into_widgets()
            self.start_tray()
            if start_hidden:
                self.withdraw()
            self.after(200, self.tick)
            self.after(500, self.refresh_recent)

        # --- layout --------------------------------------------------------

        def _card(self, parent, title=None):
            card = ctk.CTkFrame(parent, corner_radius=10)
            card.pack(fill="x", pady=(0, 12))
            if title:
                ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=13, weight="bold")
                             ).pack(anchor="w", padx=14, pady=(10, 2))
            return card

        def _build_left(self, parent):
            # Status + pause
            card = self._card(parent)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(12, 4))
            self.status_dot = ctk.CTkLabel(row, text="●", font=ctk.CTkFont(size=20))
            self.status_dot.pack(side="left")
            self.status_text = ctk.CTkLabel(row, text="Starting...",
                                            font=ctk.CTkFont(size=15, weight="bold"))
            self.status_text.pack(side="left", padx=(6, 0))
            self.pause_btn = ctk.CTkButton(row, text="Pause", width=90, command=self.toggle_pause)
            self.pause_btn.pack(side="right")
            self.counts_text = ctk.CTkLabel(card, text="", text_color="gray")
            self.counts_text.pack(anchor="w", padx=14)

            # Current file
            self.file_text = ctk.CTkLabel(card, text="", anchor="w")
            self.file_text.pack(fill="x", padx=14, pady=(8, 2))
            self.progress = ctk.CTkProgressBar(card)
            self.progress.set(0)
            self.progress.pack(fill="x", padx=14)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(2, 4))
            self.skip_btn = ctk.CTkButton(row, text="Skip this file", width=110, height=26,
                                          fg_color="transparent", border_width=1,
                                          text_color=("gray10", "gray90"),
                                          command=self.skip_file)
            self.skip_visible = False
            self.eta_text = ctk.CTkLabel(row, text="", text_color="gray", anchor="w")
            self.eta_text.pack(side="left", fill="x", expand=True)
            self.eta_row = row
            self.error_text = ctk.CTkLabel(card, text="", text_color="#d9534f",
                                           anchor="w", justify="left", wraplength=480)
            self.error_text.pack(fill="x", padx=14, pady=(0, 10))

            # Folders
            card = self._card(parent, "Folders")
            self.inbox_text = self._folder_row(card, "Inbox", "input_folder")
            self.out_text = self._folder_row(card, "Transcripts", "output_folder")
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(4, 12))
            ctk.CTkButton(row, text="Add audio files...", width=140,
                          command=self.add_files).pack(side="left")
            hint = "or drag files onto this window" if self.dnd else ""
            ctk.CTkLabel(row, text=hint, text_color="gray").pack(side="left", padx=10)
            if self.dnd:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self.on_drop)

            # Recent transcripts
            card = self._card(parent, "Recent transcripts")
            self.recent_box = ctk.CTkFrame(card, fg_color="transparent")
            self.recent_box.pack(fill="x", padx=14, pady=(0, 10))

        def _folder_row(self, card, name, key):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=2)
            ctk.CTkLabel(row, text=name, width=86, anchor="w").pack(side="left")
            ctk.CTkButton(row, text="Change", width=70, fg_color="transparent",
                          border_width=1, text_color=("gray10", "gray90"),
                          command=lambda: self.change_folder(key)).pack(side="right")
            ctk.CTkButton(row, text="Open", width=60,
                          command=lambda: open_path(self.config_raw()[key])
                          ).pack(side="right", padx=6)
            text = ctk.CTkLabel(row, text="", anchor="w", text_color="gray")
            text.pack(side="left", fill="x", expand=True)
            return text

        def _build_right(self, parent):
            card = self._card(parent, "Settings")
            body = ctk.CTkFrame(card, fg_color="transparent")
            body.pack(fill="x", padx=14, pady=(4, 12))
            body.grid_columnconfigure(1, weight=1)

            def label(r, text, hint=None):
                ctk.CTkLabel(body, text=text, anchor="w").grid(row=r, column=0, sticky="w", pady=4)
                if hint:
                    ctk.CTkLabel(body, text=hint, text_color="gray", anchor="w",
                                 font=ctk.CTkFont(size=11)
                                 ).grid(row=r + 1, column=0, columnspan=2, sticky="w")

            label(0, "Mode", "Fast is fine for clear speech; Accurate for noisy audio.")
            self.mode = ctk.CTkSegmentedButton(body, values=list(MODE_CHOICES),
                                               command=lambda v: self.save("speed", MODE_CHOICES[v]))
            self.mode.grid(row=0, column=1, sticky="ew", padx=(10, 0))

            label(2, "Quality")
            self.model = ctk.CTkOptionMenu(body, values=list(MODEL_CHOICES), dynamic_resizing=False,
                                           command=lambda v: self.save("model_size", MODEL_CHOICES[v]))
            self.model.grid(row=2, column=1, sticky="ew", padx=(10, 0))

            label(3, "Language")
            self.language = ctk.CTkOptionMenu(body, values=list(LANGUAGE_CHOICES),
                                              dynamic_resizing=False,
                                              command=self.on_language)
            self.language.grid(row=3, column=1, sticky="ew", padx=(10, 0))

            label(4, "Order")
            self.order = ctk.CTkOptionMenu(body, values=list(ORDER_CHOICES), dynamic_resizing=False,
                                           command=lambda v: self.save("order", ORDER_CHOICES[v]))
            self.order.grid(row=4, column=1, sticky="ew", padx=(10, 0))

            self.switches = {}
            rows = [
                ("vad_filter", "Skip silence", "Turn off for songs / music."),
                ("include_timestamps", "Add timestamps", None),
                ("use_filename_hint", "Use file name as a hint", "Helps spell names and titles."),
            ]
            r = 5
            for key, text, hint in rows:
                sw = ctk.CTkSwitch(body, text=text,
                                   command=lambda k=key: self.save(k, bool(self.switches[k].get())))
                sw.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
                self.switches[key] = sw
                r += 1
                if hint:
                    ctk.CTkLabel(body, text=hint, text_color="gray", font=ctk.CTkFont(size=11),
                                 anchor="w").grid(row=r, column=0, columnspan=2, sticky="w", padx=(48, 0))
                    r += 1

            self.autostart = ctk.CTkSwitch(body, text="Start with Windows", command=self.on_autostart)
            self.autostart.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
            if exe_command() is None:
                self.autostart.configure(state="disabled")

            self.saved_text = ctk.CTkLabel(card, text="", text_color="gray", anchor="w")
            self.saved_text.pack(fill="x", padx=14, pady=(0, 8))

            card = ctk.CTkFrame(parent, corner_radius=10)
            card.pack(fill="both", expand=True)
            ctk.CTkLabel(card, text="Activity", font=ctk.CTkFont(size=13, weight="bold")
                         ).pack(anchor="w", padx=14, pady=(10, 2))
            self.activity = ctk.CTkTextbox(card, font=ctk.CTkFont(family="Consolas", size=11),
                                           wrap="word", state="disabled")
            self.activity.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # --- settings ------------------------------------------------------

        def config_raw(self):
            try:
                raw = T.read_raw_config()
            except T.UserError:
                raw = {}
            merged = dict(T.DEFAULT_CONFIG)
            merged.update(raw)
            return merged

        def load_settings_into_widgets(self):
            c = self.config_raw()
            self.mode.set(label_for(MODE_CHOICES, str(c["speed"]).lower()))
            self.model.set(label_for(MODEL_CHOICES, c["model_size"]))
            lang = str(c.get("language") or "auto").lower()
            lang_label = label_for(LANGUAGE_CHOICES, lang)
            if lang_label not in LANGUAGE_CHOICES:
                self.language.configure(values=list(LANGUAGE_CHOICES) + [lang_label])
            self.language.set(lang_label)
            self.order.set(label_for(ORDER_CHOICES, str(c["order"]).lower()))
            for key, sw in self.switches.items():
                sw.select() if c.get(key) else sw.deselect()
            self.autostart.select() if get_autostart() else self.autostart.deselect()
            self.inbox_text.configure(text=self.short_path(c["input_folder"]))
            self.out_text.configure(text=self.short_path(c["output_folder"]))

        @staticmethod
        def short_path(path):
            home = str(Path.home())
            path = str(path)
            if path.lower().startswith(home.lower()):
                path = "~" + path[len(home):]
            return path if len(path) <= 40 else "..." + path[-37:]

        def save(self, key, value):
            try:
                T.update_config({key: value})
            except Exception as e:
                self.saved_text.configure(text=f"Could not save: {e}", text_color="#d9534f")
                return
            busy = self.engine.snapshot()["file"]
            note = "Saved – applies to the next file." if busy else "Saved."
            if key == "model_size":
                note = "Saved – the new model loads " + ("after this file." if busy else "now.")
            self.saved_text.configure(text=note, text_color="gray")
            log.info("Setting changed: %s = %s", key, value)
            self.engine.settings_changed()

        def on_language(self, label):
            value = LANGUAGE_CHOICES.get(label)
            if value is None:  # "Other (xx)" from config.json
                value = label[label.find("(") + 1:-1]
            self.save("language", value)

        def on_autostart(self):
            try:
                set_autostart(bool(self.autostart.get()))
            except OSError as e:
                self.saved_text.configure(text=f"Could not change: {e}", text_color="#d9534f")

        def change_folder(self, key):
            from tkinter import filedialog
            current = self.config_raw()[key]
            chosen = filedialog.askdirectory(initialdir=current if Path(current).is_dir() else None,
                                             title="Choose the Inbox folder" if key == "input_folder"
                                             else "Choose the Transcripts folder")
            if chosen:
                self.save(key, str(Path(chosen)))
                self.load_settings_into_widgets()
                self.recent_key = None

        # --- adding files ----------------------------------------------------

        def add_files(self):
            from tkinter import filedialog
            types = " ".join("*" + ext for ext in T.DEFAULT_CONFIG["file_types"])
            files = filedialog.askopenfilenames(title="Add audio files",
                                                filetypes=[("Audio and video", types),
                                                           ("All files", "*.*")])
            self.copy_to_inbox(files)

        def on_drop(self, event):
            self.copy_to_inbox(self.tk.splitlist(event.data))

        def copy_to_inbox(self, files):
            """Copy files into the Inbox in the background (big files take a while).
            The engine waits for each copy to finish before starting it."""
            files = [Path(f) for f in files if Path(f).is_file()]
            if not files:
                return
            inbox = Path(self.config_raw()["input_folder"])

            def work():
                for src in files:
                    dest = inbox / src.name
                    if dest.exists():
                        log.info("Already in the Inbox: %s", src.name)
                        continue
                    try:
                        shutil.copy2(src, dest)
                        log.info("Added to the Inbox: %s", src.name)
                    except OSError as e:
                        log.error("Could not add %s: %s", src.name, e)
                self.engine._wake.set()

            threading.Thread(target=work, daemon=True).start()

        # --- refreshing the window -----------------------------------------

        def skip_file(self):
            from tkinter import messagebox
            name = self.engine.snapshot()["file"]
            if not name:
                return
            if messagebox.askyesno(
                    "Skip this file?",
                    f"Skip {name}?\n\nProgress so far will be lost. The file stays in the "
                    "Inbox and won't be tried again unless it changes or the program "
                    "is restarted.", icon="warning", parent=self):
                if self.engine.snapshot()["file"] == name:  # still the same file
                    self.engine.skip_current()

        def toggle_pause(self):
            self.engine.set_paused(not self.engine.paused)
            if self.tray:
                self.tray.update_menu()

        def tick(self):
            """Runs 5 times a second: update the status from the engine."""
            while not self.ui_calls.empty():
                self.ui_calls.get_nowait()()
            self.drain_log()
            while not self.engine.events.empty():
                self.on_engine_event(*self.engine.events.get_nowait())

            s = self.engine.snapshot()
            colors = {"working": "#2fa84f", "idle": "#2fa84f", "paused": "#e0a800",
                      "loading": "#3b8ed0", "starting": "#3b8ed0", "error": "#d9534f"}
            titles = {"working": "Transcribing", "idle": "Running", "paused": "Paused",
                      "loading": "Loading model", "starting": "Starting",
                      "error": "Needs attention"}
            self.status_dot.configure(text_color=colors.get(s["phase"], "gray"))
            self.status_text.configure(text=titles.get(s["phase"], s["phase"]))
            self.pause_btn.configure(text="Resume" if self.engine.paused else "Pause")
            waiting = s["waiting"] - (1 if s["file"] else 0)
            self.counts_text.configure(
                text=f"{max(0, waiting)} waiting  ·  {self.done_today()} done today")

            if bool(s["file"]) != self.skip_visible:
                self.skip_visible = bool(s["file"])
                if self.skip_visible:
                    self.skip_btn.pack(side="right")
                else:
                    self.skip_btn.pack_forget()
            if s["file"]:
                self.file_text.configure(text=f"Now: {s['file']}")
                self.progress.set(s["percent"] / 100)
                left = f"  ·  about {format_duration(s['eta'])} left" if s["eta"] else ""
                self.eta_text.configure(text=f"{s['percent']:.0f}%{left}")
            else:
                self.file_text.configure(text=s["message"] if s["phase"] != "error" else "")
                self.progress.set(0)
                self.eta_text.configure(text="")
            self.error_text.configure(text=s["error"] or "")
            if self.tray:
                self.tray.title = f"{T.APP_NAME} – {titles.get(s['phase'], '')}"
            self.after(200, self.tick)

        def drain_log(self):
            lines = []
            while not self.log_lines.empty() and len(lines) < 200:
                lines.append(self.log_lines.get_nowait())
            if not lines:
                return
            self.activity.configure(state="normal")
            self.activity.insert("end", "\n".join(lines) + "\n")
            # Keep the box from growing forever.
            if int(self.activity.index("end-1c").split(".")[0]) > 500:
                self.activity.delete("1.0", "100.0")
            self.activity.see("end")
            self.activity.configure(state="disabled")

        def on_engine_event(self, kind, data):
            if kind == "skipped":
                pass
            elif kind == "done":
                self.notify("Transcript ready",
                            f"{data['audio'].name} ({format_duration(data['seconds'])})")
            else:
                self.notify("Could not transcribe", f"{data['audio'].name}: {data['error']}")
            self.recent_key = None
            self.refresh_recent()

        def output_dir(self):
            return Path(self.config_raw()["output_folder"])

        def transcripts(self):
            try:
                files = [p for p in self.output_dir().glob("*.txt") if not p.name.startswith(".")]
                return sorted(((p.stat().st_mtime, p) for p in files), reverse=True)
            except OSError:
                return []

        def done_today(self):
            today = datetime.now().date()
            return sum(1 for mtime, _ in self._recent_cache if
                       datetime.fromtimestamp(mtime).date() == today)

        _recent_cache = []

        def refresh_recent(self):
            """Every few seconds, list the newest transcripts (any PC's)."""
            items = self.transcripts()
            self._recent_cache = items
            key = tuple((p.name, int(m)) for m, p in items[:6]), int(time.time() // 60)
            if key != self.recent_key:
                self.recent_key = key
                for child in self.recent_box.winfo_children():
                    child.destroy()
                if not items:
                    import customtkinter as ctk
                    ctk.CTkLabel(self.recent_box, text="No transcripts yet.",
                                 text_color="gray").pack(anchor="w")
                for mtime, path in items[:6]:
                    self._recent_row(path, mtime)
            self.after(5000, self.refresh_recent)

        def _recent_row(self, path, mtime):
            import customtkinter as ctk
            row = ctk.CTkFrame(self.recent_box, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text="✓", text_color="#2fa84f", width=18).pack(side="left")
            name = path.name if len(path.name) <= 40 else path.name[:37] + "..."
            ctk.CTkLabel(row, text=name, anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="Open", width=56, height=24,
                          command=lambda: open_path(path)).pack(side="right")
            ctk.CTkLabel(row, text=format_when(mtime), text_color="gray"
                         ).pack(side="right", padx=8)

        # --- tray ------------------------------------------------------------

        def start_tray(self):
            try:
                import pystray
            except Exception as e:
                log.warning("Tray icon unavailable: %s", e)
                return

            def call(fn):
                return lambda icon, item: self.ui_calls.put(fn)

            menu = pystray.Menu(
                pystray.MenuItem("Open window", call(self.show_window), default=True),
                pystray.MenuItem("Pause", call(self.toggle_pause),
                                 checked=lambda item: self.engine.paused),
                pystray.MenuItem("Open Transcripts folder",
                                 call(lambda: open_path(self.output_dir()))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", call(self.quit_app)),
            )
            try:
                self.tray = pystray.Icon(RUN_NAME, make_icon_image(64), T.APP_NAME, menu)
                self.tray.run_detached()
            except Exception as e:
                self.tray = None
                log.warning("Tray icon unavailable: %s", e)

        def notify(self, title, message):
            # Only pop up a notification when the window isn't in front.
            if self.tray and (self.state() != "normal" or self.focus_displayof() is None):
                try:
                    self.tray.notify(message, title)
                except Exception:
                    pass

        def show_window(self):
            self.deiconify()
            self.lift()
            self.focus_force()

        def hide_to_tray(self):
            if not self.tray:
                self.iconify()
                return
            self.withdraw()
            if not self.told_about_tray:
                self.told_about_tray = True
                self.notify(T.APP_NAME, "Still running in the background. "
                            "Right-click the tray icon to quit.")

        def quit_app(self):
            if self.quitting:
                return
            self.quitting = True
            busy = self.engine.snapshot()["file"]
            if busy:
                log.info("Stopping (the current file will be picked up again next time)...")
            self.engine.stop()
            if self.tray:
                self.tray.stop()

            def wait_then_close():
                if self.engine.thread.is_alive() and time.time() - started < 15:
                    self.after(200, wait_then_close)
                else:
                    self.destroy()

            started = time.time()
            wait_then_close()

    return App


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def show_error(message):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, T.APP_NAME, 0x10)
    except Exception:
        print(message)


def run_console():
    """--console: the original text mode. The .exe has no console of its own,
    so open one (unless started from a command prompt that we can attach to)."""
    if getattr(sys, "frozen", False):
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ATTACH_PARENT_PROCESS = -1
        if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r")
    T.cli()


def run_window(start_hidden):
    T.setup_logging()
    log.info("=" * 60)
    log.info("%s v%s starting (window)", T.APP_NAME, T.__version__)
    try:
        config = T.load_config()
        inbox = Path(config["input_folder"])
        T.ensure_single_instance(inbox)
    except T.UserError as e:
        show_error(str(e))
        sys.exit(1)

    import customtkinter as ctk
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")

    engine = Engine(locked_folder=inbox)
    app = build_app_class()(engine, start_hidden=start_hidden)
    engine.start()
    app.mainloop()


def main():
    args = sys.argv[1:]
    if "--console" in args:
        run_console()
        return
    try:
        run_window(start_hidden="--minimized" in args)
    except SystemExit:
        raise
    except Exception:
        import traceback
        details = traceback.format_exc()
        try:
            with open(T.LOG_PATH, "a", encoding="utf-8") as f:
                f.write("FATAL ERROR (window):\n" + details)
        except OSError:
            pass
        show_error("The program stopped because of an unexpected error.\n\n"
                   f"Details were saved to:\n{T.LOG_PATH}")
        sys.exit(1)


if __name__ == "__main__":
    main()
