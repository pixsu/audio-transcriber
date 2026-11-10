"""
Audio Transcriber - window version
==================================

A control window around the transcription engine in transcriber.py:

  * status card     - what it's doing, progress, time left, Pause / Skip
  * Queue tab       - files waiting, with "Do next" and "Skip"
  * Completed tab   - finished transcripts, with Open / Show in folder
  * Issues tab      - failed or skipped files, with Retry
  * Activity tab    - live log
  * Settings panel  - easy switches for every setting (saved to config.json)
  * tray icon       - keeps running in the background, with notifications

Run with --console for the original text-only mode (e.g. on a server),
or --minimized to start hidden in the tray (used by "Start with Windows").
"""

import ctypes
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import transcriber as T

log = logging.getLogger("transcriber.app")

PROJECT_URL = "https://github.com/pixsu/audio-transcriber"

# Friendly labels for the settings -> the value stored in config.json.
MODEL_CHOICES = {
    "Tiny (fastest, rough)": "tiny",
    "Base (fast, basic)": "base",
    "Small (fast, good)": "small",
    "Medium (recommended)": "medium",
    "Turbo (more accurate, a bit slower)": "large-v3-turbo",
    "Large (best, slowest)": "large-v3",
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
DEVICE_CHOICES = {
    "Automatic (GPU if available)": "auto",
    "CPU only": "cpu",
    "NVIDIA GPU": "cuda",
}
MODE_CHOICES = {"Fast": "fast", "Accurate": "accurate"}
# Completed tab date filter -> days back (0 = since midnight, None = all)
DATE_FILTERS = {"Any time": None, "Today": 0, "Last 7 days": 7, "Last 30 days": 30}
# Settings the window manages; "Reset to defaults" restores these (not folders).
WINDOW_SETTINGS = ("speed", "model_size", "language", "order", "device",
                   "vad_filter", "include_timestamps", "label_music", "use_filename_hint",
                   "check_for_updates")

# Friendly names for the Apply confirmation and the log.
SETTING_NAMES = {
    "speed": "Mode", "model_size": "Quality", "language": "Language", "order": "Order",
    "device": "Device", "vad_filter": "Skip silence", "include_timestamps": "Add timestamps",
    "label_music": "Label music and songs",
    "use_filename_hint": "Use file name as a hint", "input_folder": "Inbox folder",
    "output_folder": "Transcripts folder", "_autostart": "Start with Windows",
    "check_for_updates": "Check for updates",
}
# Changes with bigger effects (a download, folders, Windows start-up) are
# confirmed in a dialog before they're applied.
IMPORTANT_SETTINGS = ("model_size", "device", "input_folder", "output_folder", "_autostart")
MODEL_DOWNLOAD = {"tiny": "75 MB", "base": "145 MB", "small": "480 MB",
                  "medium": "1.5 GB", "large-v3-turbo": "1.6 GB", "large-v3": "3 GB"}


def model_downloaded(size):
    # Most models come from Systran; Turbo from Mobius Labs.
    return any((T.MODELS_DIR / f"models--{owner}--faster-whisper-{size}").is_dir()
               for owner in ("Systran", "mobiuslabsgmbh"))


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AudioTranscriber"

GREEN, AMBER, BLUE, RED, GRAY = "#2e9e4f", "#d99a00", "#3b8ed0", "#d9534f", "gray"
PHASES = {  # phase -> (title, colour)
    "starting": ("Starting", BLUE),
    "loading": ("Loading model", BLUE),
    "idle": ("Ready", GREEN),
    "working": ("Transcribing", GREEN),
    "paused": ("Paused", AMBER),
    "error": ("Needs attention", RED),
    "stopping": ("Stopping", GRAY),
}


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
            "phase": "starting",
            "message": "Starting...",
            "file": None,          # name of the file being transcribed
            "percent": 0.0,
            "eta": None,           # seconds left, or None
            "queue": [],           # waiting files in order: (name, size, arrived)
            "model": None,         # e.g. "medium on CPU"
            "error": None,
        }
        self.events = queue.Queue()  # ("done"/"failed"/"skipped", data)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._restart = threading.Event()
        self._wake = threading.Event()
        self._skip = threading.Event()
        # Files not to try again this session: name -> file signature.
        # Shared with the Transcriber, and kept across settings restarts.
        self.skipped = {}
        # name -> (kind "failed"/"skipped", reason, time) for the Issues tab
        self.issues = {}
        self.priority = []  # names moved to the front with "Do next"
        self.inbox = None
        self.transcriber = None
        self._last_peek = 0.0
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
        self._update(phase="stopping", message="Stopping...")

    def wake(self):
        self._wake.set()

    def skip_current(self):
        """Abandon the file being transcribed and move on to the next one."""
        self._skip.set()
        self._paused.clear()  # a paused file can be skipped too
        self._update(message="Skipping...")

    def skip_waiting(self, name):
        """Take a waiting file out of the queue (until Retry or restart)."""
        path = self.inbox / name if self.inbox else None
        if path is None:
            return
        with self.lock:
            self.skipped[name] = T.file_signature(path)
            self.issues[name] = ("skipped", "Skipped from the queue", time.time())
            self.state["queue"] = [q for q in self.state["queue"] if q[0] != name]
            if name in self.priority:
                self.priority.remove(name)
        log.info("Skipped: %s", name)
        self._wake.set()

    def do_next(self, name):
        with self.lock:
            if name in self.priority:
                self.priority.remove(name)
            self.priority.insert(0, name)
            q = self.state["queue"]
            moved = [x for x in q if x[0] == name]
            self.state["queue"] = moved + [x for x in q if x[0] != name]
        log.info("Moved to the front of the queue: %s", name)

    def retry(self, names=None):
        with self.lock:
            for name in list(self.issues if names is None else names):
                self.issues.pop(name, None)
                self.skipped.pop(name, None)
        self._last_peek = 0.0  # show it in the Queue soon
        self._wake.set()

    def set_paused(self, paused: bool):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
            self._wake.set()
        self._update(phase="paused" if paused else ("working" if self.state["file"] else "idle"))

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def settings_changed(self):
        """Pick up config.json again (after the current file, if any)."""
        self._restart.set()
        self._wake.set()

    def snapshot(self) -> dict:
        with self.lock:
            snap = dict(self.state)
            snap["issues"] = dict(self.issues)
            return snap

    # --- Hooks (called from the engine thread) ----------------------------

    def checkpoint(self):
        if self._stop.is_set():
            raise T.Stopped()
        if self._skip.is_set():
            self._skip.clear()
            raise T.Skipped()
        if self._paused.is_set():
            began = time.time()
            while self._paused.is_set() and not (self._stop.is_set() or self._skip.is_set()):
                time.sleep(0.2)
            self._paused_for += time.time() - began
            self.checkpoint()  # stop or skip clicked while paused

    def sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()
        if self._stop.is_set():
            raise T.Stopped()

    def should_restart(self):
        return self._restart.is_set() or self._stop.is_set()

    def pick(self, ready):
        with self.lock:
            names = {p.name: p for p in ready}
            for name in self.priority:
                if name in names:
                    return names[name]
        return ready[0]

    def progress(self, audio, percent):
        # While a long file runs, look at the Inbox every 10 s so the Queue
        # tab shows newly added or retried files straight away.
        if self.transcriber and time.time() - self._last_peek > 10:
            self._last_peek = time.time()
            try:
                _, waiting, _ = self.transcriber.scan()
                self.event("queue", waiting=len(waiting),
                           files=T.sort_queue(waiting, self.transcriber.config["order"]))
            except Exception:
                pass
        worked = time.time() - self._file_started - self._paused_for
        eta = worked * (100 - percent) / percent if percent >= 2 else None
        self._update(percent=percent, eta=eta)

    def event(self, kind, **data):
        if kind == "started":
            self._skip.clear()  # a late click must not skip the next file
            self._file_started = time.time()
            self._paused_for = 0.0
            name = data["audio"].name
            with self.lock:
                if name in self.priority:
                    self.priority.remove(name)
                self.state["queue"] = [q for q in self.state["queue"] if q[0] != name]
            self._update(phase="working", file=name, percent=0.0, eta=None,
                         message=f"Transcribing {name}")
        elif kind in ("done", "failed", "skipped"):
            name = data["audio"].name
            with self.lock:
                if kind == "failed":
                    self.issues[name] = ("failed", data["error"], time.time())
                elif kind == "skipped":
                    self.issues[name] = ("skipped", "Skipped while transcribing", time.time())
                else:
                    self.issues.pop(name, None)
            self._update(file=None, percent=0.0, eta=None)
            self.events.put((kind, data))
        elif kind == "queue":
            items = []
            for p in data["files"]:
                if p.name == self.state["file"]:
                    continue
                items.append((p.name, T.get_size(p) or 0, T.arrival_time(p)))
            with self.lock:
                first = [i for n in self.priority for i in items if i[0] == n]
                items = first + [i for i in items if i not in first]
                self.state["queue"] = items
            if self.state["phase"] in ("loading", "starting"):
                self._update(phase="idle", message="Checking the Inbox...")
        elif kind == "idle":
            self._update(phase="idle", message="All caught up – waiting for new files")

    # --- internals --------------------------------------------------------

    def _update(self, **changes):
        with self.lock:
            if self._paused.is_set() and changes.get("phase") in ("working", "idle"):
                changes["phase"] = "paused"
            if self._stop.is_set() and "phase" in changes:
                changes["phase"] = "stopping"
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
                    try:
                        folder.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        raise T.UserError(
                            f"Can't use the folder {folder} ({e.strerror or e}).\n"
                            "Choose another folder under Settings → Folders.") from None
                if input_dir != locked_folder:
                    T.ensure_single_instance(input_dir)
                    locked_folder = input_dir
                    with self.lock:  # different Inbox: forget the old queue
                        self.priority.clear()
                        self.state["queue"] = []
                self.inbox = input_dir

                key = (config["model_size"], config["device"])
                if key != model_key:
                    model = None  # free the old model's memory first
                    self._update(phase="loading", error=None,
                                 message=f"Loading the {config['model_size']} model – the "
                                         "first time it is downloaded, which can take a while...")
                    model = T.load_model(config)
                    model_key = key
                    where = "GPU" if getattr(getattr(model, "model", None), "device", "cpu") == "cuda" else "CPU"
                    self._update(model=f"{config['model_size']} on {where}")

                self._update(phase="idle", error=None, message="Checking the Inbox...")
                self.transcriber = T.Transcriber(model, config, self, failed=self.skipped)
                self.transcriber.run_forever()
            except T.Stopped:
                break
            except Exception as e:  # a UserError or anything unexpected
                if not isinstance(e, T.UserError):
                    T.file_only.error("Engine error:\n%s", T.traceback.format_exc())
                    model_key = None  # reload everything on retry
                log.error("%s", e)
                self._update(phase="error", error=str(e), file=None,
                             message="Stopped – fix the problem below, then click Try again")
                # Wait for a settings change or "Try again", then retry.
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


def make_chevron(color, size=48):
    """A thin downward chevron for the dropdowns."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(2, size // 9)
    d.line([(size * 0.22, size * 0.38), (size * 0.5, size * 0.64), (size * 0.78, size * 0.38)],
           fill=color, width=w, joint="curve")
    return img


def save_ico(path):
    """Write the icon as a .ico file (used by build.bat for the .exe)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    make_icon_image(256).save(path, sizes=[(16, 16), (24, 24), (32, 32),
                                           (48, 48), (64, 64), (256, 256)])


LINE_TIME = re.compile(r"\[(\d\d:\d\d:\d\d) → (\d\d:\d\d:\d\d)\]\s*(.*)")
ANY_TIME = re.compile(r"\[\d\d:\d\d:\d\d → \d\d:\d\d:\d\d\]")


def fix_line(line, fix):
    """Apply a text fix to one transcript line, leaving its [MUSIC 00:00:00 →
    ...] / [SILENCE ...] label untouched."""
    if line.startswith("[") and "] " in line:
        label, text = line.split("] ", 1)
        return f"{label}] {fix(text)}"
    if line.startswith("[SILENCE"):
        return line
    return fix(line)


def fix_transcript_file(path, fix):
    """Rewrite a transcript with a fix applied. Returns how many lines changed."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    new = [fix_line(l, fix) for l in lines]
    changed = sum(1 for a, b in zip(lines, new) if a != b)
    if changed:
        tmp = Path(path).with_name(Path(path).name + f".{T.HOSTNAME}.fix")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(new))
        os.replace(tmp, path)
    return changed


def count_in_transcript(path, fix):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return 0
    return sum(1 for l in lines if fix_line(l, fix) != l)


def open_path(path):
    try:
        os.startfile(str(path))
    except OSError as e:
        log.error("Could not open %s: %s", path, e)


def show_in_folder(path):
    """Open Explorer with the file selected."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError as e:
        log.error("Could not open Explorer: %s", e)


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


# How long each transcript took, kept in the Transcripts folder so it
# survives restarts and is shared between PCs. One file per PC, so two PCs
# never write the same file.
def stats_path(folder):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in T.HOSTNAME)
    return Path(folder) / f".timings-{safe}.json"


def load_timings(folder):
    import json
    took = {}
    try:
        files = list(Path(folder).glob(".timings-*.json"))
    except OSError:
        return took
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                took.update({k: float(v) for k, v in json.load(fh).items()})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return took


def save_timing(folder, txt_name, seconds):
    import json
    path = stats_path(folder)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[txt_name] = round(seconds, 1)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("Couldn't save timing: %s", e)


UPDATE_DOWNLOAD_DIR_NAME = "AudioTranscriber-update"


def clean_old_updates():
    """Delete installers left over from earlier one-click updates."""
    import tempfile
    folder = Path(tempfile.gettempdir()) / UPDATE_DOWNLOAD_DIR_NAME
    if not folder.is_dir():
        return
    freed = 0
    for f in folder.iterdir():
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError:
            pass  # still in use (e.g. the installer is running right now)
    try:
        folder.rmdir()
    except OSError:
        pass
    if freed:
        log.info("Removed an old update download (%.0f MB)", freed / 1e6)


def system_summary():
    """Plain-text facts about this PC, for problem reports."""
    import platform
    lines = [f"{T.APP_NAME} {T.__version__}",
             f"Created: {datetime.now():%Y-%m-%d %H:%M}",
             f"PC name: {T.HOSTNAME}",
             f"Windows: {platform.platform()}",
             f"Processor: {platform.processor()} ({os.cpu_count()} threads)",
             f"Program folder: {T.APP_DIR}",
             f"Installed with installer: {can_self_update()}"]
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        lines.append(f"Memory: {m.ullTotalPhys / 2**30:.1f} GB "
                     f"({m.ullAvailPhys / 2**30:.1f} GB free)")
    except Exception:
        pass
    try:
        free = shutil.disk_usage(T.APP_DIR).free
        lines.append(f"Free disk space (program drive): {free / 2**30:.1f} GB")
    except OSError:
        pass
    return "\n".join(lines)


def make_report_zip(issues, target_dir):
    """Zip the log, settings and PC details (never audio or transcripts)."""
    import zipfile
    name = f"AudioTranscriber-report-{T.HOSTNAME}-{datetime.now():%Y%m%d-%H%M}.zip"
    target = Path(target_dir) / name
    issue_lines = [f"{kind.upper()}  {name_}  ({datetime.fromtimestamp(when):%Y-%m-%d %H:%M})"
                   f"\n    {reason}" for name_, (kind, reason, when) in issues.items()]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about-this-pc.txt", system_summary())
        z.writestr("issues.txt", "\n".join(issue_lines) or "No issues listed.")
        if T.CONFIG_PATH.exists():
            z.write(T.CONFIG_PATH, "config.json")
        for log_file in sorted(T.APP_DIR.glob(T.LOG_PATH.name + "*")):
            z.write(log_file, log_file.name)
    return target


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


def format_size(size):
    if size >= 1_000_000_000:
        return f"{size / 1e9:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1e6:.1f} MB" if size < 100_000_000 else f"{size / 1e6:.0f} MB"
    return f"{max(1, size // 1000)} KB"


def shorten(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def short_path(path, limit=44):
    home = str(Path.home())
    path = str(path)
    if path.lower().startswith(home.lower()):
        path = "~" + path[len(home):]
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


def friendly_error(reason):
    """Turn a technical error into a short plain-English reason.
    (The full error is always in the log file.)"""
    text = str(reason)
    low = text.lower()
    if "invalid data found" in low or "could not find codec" in low or "end of file" in low:
        return "The file is damaged or isn't a real audio file."
    if "no such file" in low or "cannot find the file" in low or "filenotfound" in low:
        return "The file was moved or deleted before it could be transcribed."
    if "permission denied" in low or "access is denied" in low or "being used by another" in low:
        return "The file couldn't be opened. It may be open in another program."
    if "no space left" in low or "not enough space" in low:
        return "The disk is full."
    if "out of memory" in low or "memoryerror" in low or "bad_alloc" in low:
        return "Not enough memory. Try a smaller Quality setting."
    first = text.splitlines()[0] if text else "Unknown error"
    return shorten(first, 120) + "  (see the log file for details)"


# --- Update check -------------------------------------------------------------

UPDATE_URL = "https://api.github.com/repos/pixsu/audio-transcriber/releases/latest"
RELEASE_DOWNLOAD_PREFIX = "https://github.com/pixsu/audio-transcriber/releases/download/"
UPDATE_CHECK_EVERY = 24 * 3600


def version_tuple(text):
    import re
    return tuple(int(n) for n in re.findall(r"\d+", str(text))[:3])


def fetch_latest_release(url=UPDATE_URL):
    """Ask GitHub for the newest release.

    Returns {"version", "page", "download"}. Raises an error with a
    plain-English message if it can't be checked.
    """
    import json
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={
        "User-Agent": f"AudioTranscriber/{T.__version__}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("No public release was found. (The GitHub repository "
                               "must be public for update checks to work.)") from None
        raise RuntimeError(f"The update server answered with error {e.code}.") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("Couldn't reach the update server. Check the internet "
                           "connection.") from None
    version = str(data.get("tag_name", "")).lstrip("vV")
    download = data.get("html_url", "")
    setup = None
    assets = data.get("assets", [])
    for asset in assets:
        name = asset.get("name", "").lower()
        if "setup" in name and name.endswith(".exe"):
            download = asset.get("browser_download_url", download)
            setup = asset
            break
    info = {"version": version, "page": data.get("html_url", ""), "download": download,
            "setup_url": None, "setup_size": 0, "sha256": None}
    # One-click updates need the installer from this repository and its
    # SHA-256 fingerprint; without them the notice offers a normal download.
    if setup and str(setup.get("browser_download_url", "")).startswith(RELEASE_DOWNLOAD_PREFIX):
        sha = None
        digest = str(setup.get("digest") or "")
        if digest.startswith("sha256:"):
            sha = digest[7:]
        for asset in assets:
            if asset.get("name", "").lower() == setup.get("name", "").lower() + ".sha256":
                try:
                    with urllib.request.urlopen(urllib.request.Request(
                            asset["browser_download_url"],
                            headers={"User-Agent": f"AudioTranscriber/{T.__version__}"}),
                            timeout=15) as r:
                        sha = r.read(4096).decode("ascii", "ignore").split()[0]
                except (urllib.error.URLError, OSError, IndexError):
                    pass
        if sha and len(sha) == 64:
            info.update(setup_url=setup["browser_download_url"],
                        setup_size=int(setup.get("size") or 0), sha256=sha.lower())
    return info


def can_self_update():
    """True when this copy was installed with the installer (not the zip or
    the source code), so the update can be installed in place."""
    return getattr(sys, "frozen", False) and any(T.APP_DIR.glob("unins*.exe"))


def download_update(info, progress):
    """Download the installer to a temp folder and check its fingerprint.
    Returns the file's path. progress(fraction) is called while downloading."""
    import hashlib
    import tempfile
    import urllib.request
    folder = Path(tempfile.gettempdir()) / UPDATE_DOWNLOAD_DIR_NAME
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.exe"):
        try:
            old.unlink()
        except OSError:
            pass
    target = folder / f"AudioTranscriber-Setup-{info['version']}.exe"
    sha = hashlib.sha256()
    done = 0
    request = urllib.request.Request(info["setup_url"], headers={
        "User-Agent": f"AudioTranscriber/{T.__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as r, open(target, "wb") as f:
            total = int(r.headers.get("Content-Length") or info["setup_size"] or 0)
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if total:
                    progress(min(done / total, 1.0))
    except OSError:
        raise RuntimeError("The download stopped. Check the internet connection "
                           "and try again.") from None
    if sha.hexdigest() != info["sha256"]:
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError("The downloaded update didn't pass the safety check "
                           "(its fingerprint doesn't match), so it wasn't installed.")
    return target


# --- File details (duration, format, ...) ----------------------------------

COVER_ART_CODECS = {"mjpeg", "png", "bmp", "gif", "webp"}


def probe_media(path):
    """Read the basic details of an audio/video file (fast: headers only)."""
    info = {"format": path.suffix.lstrip(".").upper() or "?"}
    try:
        info["size"] = path.stat().st_size
    except OSError:
        info["size"] = 0
    try:
        import av
        with av.open(str(path)) as c:
            if c.duration:
                info["duration"] = c.duration / 1_000_000
            if c.bit_rate:
                info["bitrate"] = c.bit_rate
            if c.streams.audio:
                cc = c.streams.audio[0].codec_context
                info["codec"] = (cc.name or "").upper()
                info["sample_rate"] = cc.sample_rate
                layout = getattr(cc, "layout", None)
                info["channels"] = (getattr(layout, "nb_channels", None)
                                    or getattr(cc, "channels", None))
            else:
                info["error"] = "no audio track"
            # Cover art inside an MP3 shows up as a "video" stream; ignore it.
            info["video"] = any(v.codec_context.name not in COVER_ART_CODECS
                                for v in c.streams.video)
    except Exception:
        info["error"] = "details unavailable"
    return info


def read_text_lower(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        return {"text": text, "lower": text.lower()}
    except OSError:
        return {}


def count_words(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {"words": len(f.read().split())}
    except OSError:
        return {}


class DetailsCache:
    """Reads file details in a background thread and remembers them.

    get() returns the details, or None while they're still being read.
    A file that changes (size or modified time) is read again.
    """

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.todo = queue.Queue()
        self.queued = set()
        self.version = 0  # goes up whenever new details arrive
        threading.Thread(target=self._work, name="details", daemon=True).start()

    def get(self, path, reader=probe_media):
        try:
            st = path.stat()
        except OSError:
            return None
        key = (reader.__name__, str(path).lower(), st.st_size, int(st.st_mtime))
        with self.lock:
            if key in self.data:
                return self.data[key]
            if key not in self.queued:
                self.queued.add(key)
                self.todo.put((key, path, reader))
        return None

    def _work(self):
        while True:
            key, path, reader = self.todo.get()
            try:
                info = reader(path)
            except Exception:
                info = {"error": "details unavailable"}
            with self.lock:
                self.data[key] = info
                self.queued.discard(key)
                self.version += 1


def format_clock(seconds):
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def media_summary(info, full=False):
    """e.g. "MP3  ·  1:05:27  ·  94 MB" (full adds bitrate, channels, sample rate)."""
    if info is None:
        return "Reading details..."
    kind = info.get("format", "?")
    if info.get("video"):
        kind += " video"
    parts = [kind]
    if info.get("duration"):
        parts.append(format_clock(info["duration"]))
    if info.get("size"):
        parts.append(format_size(info["size"]))
    if full:
        if info.get("bitrate"):
            parts.append(f"{round(info['bitrate'] / 1000)} kbps")
        ch = info.get("channels")
        if ch:
            parts.append({1: "Mono", 2: "Stereo"}.get(ch, f"{ch} channels"))
        if info.get("sample_rate"):
            parts.append(f"{info['sample_rate'] / 1000:g} kHz")
    if info.get("error"):
        parts.append(info["error"])
    return "  \u00b7  ".join(parts)


def label_for(choices, value):
    for label, v in choices.items():
        if v == value:
            return label
    return f"Other ({value})"


def exe_command():
    """Command line for "Start with Windows", or None when running from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return None


def get_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return value
    except OSError:
        return None


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
    """Sends log lines to the window's Activity tab."""

    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put((record.levelno, self.format(record)))
        except Exception:
            pass


# --- "Show the window" signal between two copies of the program -----------
# Starting the program again while it's already running (e.g. hidden in the
# tray) brings the running window to the front instead of showing an error.

def _show_event_name(inbox):
    return f"Local\\AudioTranscriber-show-{T.folder_key(inbox)}"


def listen_for_event(name, callback):
    """Call callback() (in a background thread) whenever the named Windows
    event is signalled by another process."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool,
                                      ctypes.c_wchar_p)
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    handle = kernel32.CreateEventW(None, False, False, name)
    if not handle:
        return

    def wait():
        while True:
            if kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
                callback()

    threading.Thread(target=wait, name="event-listener", daemon=True).start()


def listen_for_show_requests(inbox, callback):
    listen_for_event(_show_event_name(inbox), callback)


# --- Closing the program from the installer ---------------------------------
# The installer runs "AudioTranscriber.exe --quit" before replacing files.
# That signals this event; the running window quits cleanly (a file in
# progress is simply started again next time).

RUNNING_MUTEX = "AudioTranscriberAppRunning"
QUIT_EVENT = "Local\\AudioTranscriber-quit"


def ask_running_copies_to_quit(timeout=30):
    """--quit: ask every running window to close, and wait until they have."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    EVENT_MODIFY_STATE, SYNCHRONIZE = 0x0002, 0x00100000
    deadline = time.time() + timeout
    while time.time() < deadline:
        running = kernel32.OpenMutexW(SYNCHRONIZE, False, RUNNING_MUTEX)
        if not running:
            return True  # nothing running any more
        kernel32.CloseHandle(running)
        event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT)
        if event:
            kernel32.SetEvent(event)
            kernel32.CloseHandle(event)
        time.sleep(0.5)
    return False


def ask_running_copy_to_show(inbox):
    """True if another copy (window version) was told to show itself."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        EVENT_MODIFY_STATE = 0x0002
        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, _show_event_name(inbox))
        if not handle:
            return False
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def build_app_class():
    """Create the window class (drag-and-drop support if tkinterdnd2 loads)."""
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        base = (ctk.CTk, TkinterDnD.DnDWrapper)
    except Exception:
        TkinterDnD, DND_FILES = None, None
        base = (ctk.CTk,)

    def font(size=13, weight="normal"):
        return ctk.CTkFont(size=size, weight=weight)

    def ghost_button(parent, text, command, width=70, height=26):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                             fg_color="transparent", border_width=1,
                             border_color=("gray70", "gray35"),
                             text_color=("gray10", "gray90"),
                             hover_color=("gray85", "gray25"))

    FIELD = ("gray97", "gray20")
    FIELD_HOVER = ("gray92", "gray25")
    FIELD_BORDER = ("gray75", "gray32")
    open_popup = []  # the dropdown list that is open, if any

    def close_popups(event=None):
        while open_popup:
            open_popup.pop().close()

    class Select(ctk.CTkFrame):
        """A dropdown that matches the rest of the window (the standard one
        opens a plain Windows menu with a white border)."""

        TRANSPARENT = "#010203"  # used to give the pop-up rounded corners

        def __init__(self, master, values, command=None):
            super().__init__(master, height=34, corner_radius=8, border_width=1,
                             fg_color=FIELD, border_color=FIELD_BORDER)
            self.pack_propagate(False)
            self.values, self.command = list(values), command
            self.value, self.popup = None, None
            self.text = ctk.CTkLabel(self, text="", anchor="w")
            self.text.pack(side="left", fill="x", expand=True, padx=(12, 4))
            self.arrow = ctk.CTkLabel(self, text="", width=16, image=ctk.CTkImage(
                make_chevron((90, 90, 90)), make_chevron((170, 170, 170)), size=(14, 14)))
            self.arrow.pack(side="right", padx=(0, 10))
            for w in (self, self.text, self.arrow):
                w.bind("<Button-1>", self.toggle)
                w.bind("<Enter>", lambda e: self.configure(fg_color=FIELD_HOVER))
                w.bind("<Leave>", lambda e: self.configure(
                    fg_color=FIELD_HOVER if self.popup else FIELD))
                try:
                    w.configure(cursor="hand2")
                except Exception:
                    pass

        def set(self, value):
            self.value = value
            self.text.configure(text=value)

        def get(self):
            return self.value

        def set_values(self, values):
            self.values = list(values)

        def toggle(self, event=None):
            if self.popup:
                self.close()
            else:
                close_popups()
                self.open()

        def open(self):
            top = tk.Toplevel(self)
            top.withdraw()
            top.title("Audio Transcriber dropdown")
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=self.TRANSPARENT)
            try:
                top.wm_attributes("-transparentcolor", self.TRANSPARENT)
            except tk.TclError:
                pass
            box = ctk.CTkFrame(top, corner_radius=10, border_width=1,
                               fg_color=("white", "gray17"), border_color=FIELD_BORDER,
                               bg_color=self.TRANSPARENT)
            box.pack(fill="both", expand=True)
            last = len(self.values) - 1
            for i, value in enumerate(self.values):
                chosen = value == self.value
                ctk.CTkButton(box, text=value,
                              anchor="w", height=32, corner_radius=6,
                              font=font(13, "bold" if chosen else "normal"),
                              fg_color=("gray90", "gray24") if chosen else "transparent",
                              hover_color=("gray85", "gray28"),
                              text_color=("gray10", "gray92"),
                              command=lambda v=value: self.choose(v)
                              ).pack(fill="x", padx=6, pady=(6 if i == 0 else 1,
                                                             6 if i == last else 0))
            top.update_idletasks()
            width, height = self.winfo_width(), top.winfo_reqheight()
            x = self.winfo_rootx()
            y = self.winfo_rooty() + self.winfo_height() + 4
            if y + height > self.winfo_screenheight() - 40:  # no room below: open upwards
                y = self.winfo_rooty() - height - 4
            top.geometry(f"{width}x{height}+{x}+{y}")
            top.deiconify()
            self.popup = top
            open_popup.append(self)
            self.configure(border_color=("#3B8ED0", "#1F6AA5"))

        def owns(self, widget):
            """True if a widget is part of this dropdown or its open list."""
            name = str(widget)
            return name.startswith(str(self)) or bool(
                self.popup and name.startswith(str(self.popup)))

        def close(self):
            if self.popup:
                self.popup.destroy()
                self.popup = None
            if self in open_popup:
                open_popup.remove(self)
            self.configure(fg_color=FIELD, border_color=FIELD_BORDER)

        def choose(self, value):
            self.close()
            if value != self.value:
                self.set(value)
                if self.command:
                    self.command(value)

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
            self._batch_file, self._batch_done = None, 0
            self.quitting = False
            self.copying = 0                # files being copied into the Inbox
            self.took = {}                  # transcript name -> seconds (this session)
            self.view_keys = {}             # tab -> what it currently shows
            self.completed = []             # [(mtime, Path)] newest first
            self.last_completed_scan = 0.0
            self.progress_mode = "determinate"
            self.pending = {}               # settings changed but not applied yet
            self.details = DetailsCache()   # duration, format, words, ...
            self.details_stamp = 0          # details version the lists were drawn with
            self.details_checked = 0.0
            self.source_of = {}             # transcript name (lowercase) -> audio name
            self.update_info = None         # newest release, if newer than this one
            self.search_typed = 0.0         # when the search box last changed
            self.search_query = ""          # the search actually applied
            self.just_applied = False       # showing "Applied" for a few seconds

            handler = QueueLogHandler(self.log_lines)
            handler.addFilter(T.setup_logging.only_ours)
            logging.getLogger().addHandler(handler)

            self.title(f"{T.APP_NAME} {T.__version__}")
            self.geometry("1140x740")
            self.minsize(1000, 660)
            try:
                from PIL import ImageTk
                self._icon = ImageTk.PhotoImage(make_icon_image(64))
                self.iconphoto(True, self._icon)
                self._header_icon = ctk.CTkImage(make_icon_image(64), size=(28, 28))
            except Exception:
                self._header_icon = None
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.grid_columnconfigure(0, weight=1)
            self.grid_columnconfigure(1, weight=0, minsize=400)
            self.grid_rowconfigure(1, weight=1)
            self._build_header()
            left = ctk.CTkFrame(self, fg_color="transparent")
            left.grid(row=1, column=0, sticky="nsew", padx=(16, 8), pady=(0, 8))
            self._build_update_banner(left)
            self._build_status_card(left)
            self._build_tabs(left)
            self._build_settings()
            self._build_status_bar()

            if self.dnd:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self.on_drop)
            self.bind("<Control-o>", lambda e: self.add_files())
            self.bind_all("<MouseWheel>", close_popups, add="+")
            self.bind_all("<Escape>", close_popups, add="+")
            self.bind_all("<Button-1>", self._click_anywhere, add="+")
            self.bind("<Configure>", lambda e: close_popups() if e.widget is self else None,
                      add="+")
            self.bind("<Control-q>", lambda e: self.quit_app())

            self.load_settings_into_widgets()
            self.start_tray()
            if start_hidden:
                self.withdraw()
            self.after(200, self.tick)
            self.after(8000, self.schedule_update_check)
            threading.Thread(target=clean_old_updates, name="cleanup", daemon=True).start()

        # =================================================================
        # Layout
        # =================================================================

        def _build_header(self):
            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(12, 10))
            ctk.CTkLabel(bar, text="", image=self._header_icon).pack(side="left")
            ctk.CTkLabel(bar, text=T.APP_NAME, font=font(18, "bold")).pack(side="left", padx=(10, 6))
            ctk.CTkLabel(bar, text=f"v{T.__version__}", text_color=GRAY).pack(side="left", pady=(4, 0))

            self.menu_btn = ghost_button(bar, "☰  Menu", self.open_menu, width=86, height=30)
            self.menu_btn.pack(side="right")
            ghost_button(bar, "Open Transcripts", lambda: open_path(self.output_dir()),
                         width=130, height=30).pack(side="right", padx=6)
            ghost_button(bar, "Open Inbox", lambda: open_path(self.input_dir()),
                         width=96, height=30).pack(side="right")
            ctk.CTkButton(bar, text="＋  Add files", width=110, height=30,
                          command=self.add_files).pack(side="right", padx=6)

            self.menu = tk.Menu(self, tearoff=False)
            self.menu.add_command(label="Add files...", accelerator="Ctrl+O", command=self.add_files)
            self.menu.add_command(label="Open Inbox folder", command=lambda: open_path(self.input_dir()))
            self.menu.add_command(label="Open Transcripts folder",
                                  command=lambda: open_path(self.output_dir()))
            self.menu.add_separator()
            self.menu.add_command(label="Retry all issues", command=self.retry_all)
            self.menu.add_command(label="Open log file", command=lambda: open_path(T.LOG_PATH))
            self.menu.add_command(label="Create problem report...", command=self.send_report)
            self.menu.add_command(label="Edit config.json (advanced)", command=self.edit_config)
            self.menu.add_separator()
            self.menu.add_command(label="Check for updates", command=self.check_updates_now)
            self.menu.add_command(label="About", command=self.show_about)
            self.menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.quit_app)

        def _build_update_banner(self, parent):
            self.banner = ctk.CTkFrame(parent, corner_radius=12, border_width=1,
                                       fg_color=("#e8f1fb", "#1d2b3a"),
                                       border_color=("#9cc3ea", "#2f5378"))
            # Buttons are packed first so they always keep their full size;
            # the text gets the space that's left.
            ghost_button(self.banner, "Later", self.dismiss_update, width=64, height=30
                         ).pack(side="right", padx=(4, 12), pady=10)
            ghost_button(self.banner, "What's new", lambda: self.open_url(
                (self.update_info or {}).get("page")), width=96, height=30
                         ).pack(side="right", padx=4, pady=10)
            self.update_btn = ctk.CTkButton(self.banner, text="Update now", width=120,
                                            height=30, command=self.start_update)
            self.update_btn.pack(side="right", padx=4, pady=10)
            # Two short lines (title + detail) so the text fits next to the
            # buttons even in a narrow window.
            texts = ctk.CTkFrame(self.banner, fg_color="transparent")
            texts.pack(side="left", fill="x", expand=True, padx=16, pady=8)
            self.banner_text = ctk.CTkLabel(texts, text="", anchor="w", font=font(13, "bold"),
                                            height=18)
            self.banner_text.pack(fill="x")
            self.banner_sub = ctk.CTkLabel(texts, text="", anchor="w", text_color=GRAY,
                                           font=font(11), height=16)
            self.banner_sub.pack(fill="x")
            self.update_state = None   # None, "downloading", "waiting", "installing"

        def _build_status_card(self, parent):
            card = ctk.CTkFrame(parent, corner_radius=12)
            card.pack(fill="x", pady=(0, 12))
            self.status_card = card
            card.grid_columnconfigure(0, weight=1)

            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
            self.pill = ctk.CTkLabel(top, text="  Starting  ", corner_radius=10,
                                     fg_color=BLUE, text_color="white", font=font(12, "bold"),
                                     height=24)
            self.pill.pack(side="left")
            self.counts_text = ctk.CTkLabel(top, text="", text_color=GRAY)
            self.counts_text.pack(side="left", padx=12)
            self.pause_btn = ctk.CTkButton(top, text="Pause", width=96, height=30,
                                           command=self.toggle_pause)
            self.pause_btn.pack(side="right")
            self.skip_btn = ghost_button(top, "Skip this file", self.skip_file, width=110, height=30)
            self.retry_btn = ctk.CTkButton(top, text="Try again", width=96, height=30,
                                           fg_color=RED, hover_color="#b52b27",
                                           command=self.engine.settings_changed)

            self.file_text = ctk.CTkLabel(card, text="Starting...", font=font(15, "bold"),
                                          anchor="w")
            self.file_text.grid(row=1, column=0, sticky="ew", padx=16, pady=(12, 0))
            self.meta_text = ctk.CTkLabel(card, text="", text_color=GRAY, anchor="w",
                                          font=font(12))
            self.meta_text.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 6))
            self.progress = ctk.CTkProgressBar(card, height=10)
            self.progress.set(0)
            self.progress.grid(row=3, column=0, sticky="ew", padx=16)
            self.detail_text = ctk.CTkLabel(card, text="", text_color=GRAY, anchor="w",
                                            justify="left", wraplength=560)
            self.detail_text.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 14))

        def _build_tabs(self, parent):
            box = ctk.CTkFrame(parent, corner_radius=12)
            box.pack(fill="both", expand=True)
            self.tab_names = ["Queue", "Completed", "Issues", "Activity"]
            self.current_tab = "Queue"
            self.tabs = ctk.CTkSegmentedButton(box, values=self.tab_names,
                                               command=self.on_tab)
            self.tabs.set("Queue")
            self.tabs.pack(anchor="w", padx=12, pady=(12, 8))

            self.tab_frames = {}   # what is shown for each tab
            self.list_frames = {}  # where each tab's rows go
            for name in ("Queue", "Completed", "Issues"):
                frame = ctk.CTkScrollableFrame(box, fg_color="transparent")
                self.tab_frames[name] = self.list_frames[name] = frame

            # Completed: a search bar above the list.
            holder = ctk.CTkFrame(box, fg_color="transparent")
            bar = ctk.CTkFrame(holder, fg_color="transparent")
            bar.pack(fill="x", padx=8, pady=(0, 6))
            # (No textvariable: with one, CTkEntry never shows its grey hint text.)
            self.search_entry = ctk.CTkEntry(
                bar, height=34, corner_radius=8,
                placeholder_text="Search by name or by words in the transcript...")
            self.search_entry.pack(side="left", fill="x", expand=True)
            self.search_entry.bind("<KeyRelease>",
                                   lambda e: setattr(self, "search_typed", time.time()))
            self.search_entry.bind("<Escape>", self.clear_search)
            self.date_filter = Select(bar, values=list(DATE_FILTERS),
                                      command=lambda v: self.view_keys.pop("Completed", None))
            self.date_filter.configure(width=150)
            self.date_filter.set("Any time")
            self.date_filter.pack(side="left", padx=(8, 0))
            self.result_text = ctk.CTkLabel(holder, text="", text_color=GRAY, font=font(11),
                                            anchor="w")
            self.result_text.pack(fill="x", padx=12)
            self.list_frames["Completed"].destroy()
            self.list_frames["Completed"] = ctk.CTkScrollableFrame(holder, fg_color="transparent")
            self.list_frames["Completed"].pack(fill="both", expand=True)
            self.tab_frames["Completed"] = holder
            self.activity = ctk.CTkTextbox(box, font=ctk.CTkFont(family="Consolas", size=11),
                                           wrap="word", state="disabled", fg_color="transparent")
            self.activity.tag_config("warn", foreground=AMBER)
            self.activity.tag_config("error", foreground=RED)
            self.tab_frames["Activity"] = self.activity
            self.tab_frames["Queue"].pack(fill="both", expand=True, padx=6, pady=(0, 8))

        def on_tab(self, label):
            name = label.split(" ")[0]
            if name == self.current_tab:
                return
            self.tab_frames[self.current_tab].pack_forget()
            self.tab_frames[name].pack(fill="both", expand=True, padx=6, pady=(0, 8))
            self.current_tab = name
            self.view_keys.pop(name, None)
            self.refresh_tabs(force=True)

        def _section(self, parent, title):
            ctk.CTkLabel(parent, text=title.upper(), font=font(11, "bold"), text_color=GRAY,
                         anchor="w").pack(fill="x", padx=4, pady=(14, 4))
            card = ctk.CTkFrame(parent, corner_radius=10)
            card.pack(fill="x")
            card.grid_columnconfigure(1, weight=1)
            return card

        def _field(self, card, row, label, widget_factory, hint=None):
            """A setting stacked as: label, full-width control, hint."""
            box = ctk.CTkFrame(card, fg_color="transparent")
            box.grid(row=row, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 0))
            ctk.CTkLabel(box, text=label, anchor="w", font=font(13, "bold")
                         ).pack(fill="x")
            widget = widget_factory(box)
            widget.pack(fill="x", pady=(4, 0))
            if hint:
                ctk.CTkLabel(box, text=hint, text_color=GRAY, font=font(11), anchor="w",
                             justify="left", wraplength=330).pack(fill="x", pady=(3, 0))
            return widget

        def _switch(self, card, row, text, key, hint=None, command=None):
            sw = ctk.CTkSwitch(card, text=text,
                               command=command or (lambda: self.stage(key, bool(sw.get()))))
            sw.grid(row=row, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 0))
            if hint:
                ctk.CTkLabel(card, text=hint, text_color=GRAY, font=font(11), anchor="w",
                             justify="left", wraplength=290
                             ).grid(row=row + 1, column=0, columnspan=2, sticky="w", padx=(58, 12))
            return sw

        def _build_settings(self):
            outer = ctk.CTkFrame(self, corner_radius=12)
            outer.grid(row=1, column=1, sticky="nsew", padx=(8, 16), pady=(0, 8))
            ctk.CTkLabel(outer, text="Settings", font=font(16, "bold"), anchor="w"
                         ).pack(fill="x", padx=18, pady=(14, 0))
            ctk.CTkLabel(outer, text="Changes take effect when you click Apply.", text_color=GRAY,
                         font=font(11), anchor="w").pack(fill="x", padx=18)

            # Apply / Discard bar, always at the bottom of the panel.
            bar = ctk.CTkFrame(outer, fg_color="transparent")
            bar.pack(side="bottom", fill="x", padx=14, pady=(6, 12))
            ctk.CTkFrame(outer, height=1, fg_color=("gray80", "gray30")
                         ).pack(side="bottom", fill="x", padx=12)
            self.apply_btn = ctk.CTkButton(bar, text="Apply", width=90, height=32,
                                           command=self.apply_changes)
            self.apply_btn.pack(side="right")
            self.discard_btn = ghost_button(bar, "Discard", self.discard_changes,
                                            width=80, height=32)
            self.discard_btn.pack(side="right", padx=8)
            self.pending_text = ctk.CTkLabel(bar, text="", anchor="w", font=font(12))
            self.pending_text.pack(side="left")

            panel = ctk.CTkScrollableFrame(outer, width=360, fg_color="transparent")
            panel.pack(fill="both", expand=True, padx=(6, 4), pady=(0, 4))

            card = self._section(panel, "Transcription")
            self.mode = self._field(card, 0, "Mode", lambda p: ctk.CTkSegmentedButton(
                p, values=list(MODE_CHOICES),
                command=lambda v: self.stage("speed", MODE_CHOICES[v])),
                "Fast is right for clear speech. Accurate can help with noisy audio, "
                "music or strong accents (about 35% slower).")
            self.model = self._field(card, 2, "Quality", lambda p: Select(
                p, values=list(MODEL_CHOICES),
                command=lambda v: self.stage("model_size", MODEL_CHOICES[v])),
                "Bigger is more accurate but slower. A new model is downloaded once.")
            self.language = self._field(card, 4, "Language", lambda p: Select(
                p, values=list(LANGUAGE_CHOICES),
                command=self.on_language),
                "Auto-detect handles English, Filipino and Taglish.")
            ctk.CTkFrame(card, height=8, fg_color="transparent").grid(row=6, column=0)

            card = self._section(panel, "Options")
            self.switches = {
                "vad_filter": self._switch(card, 0, "Skip silence", "vad_filter",
                                           "Faster on talk shows and meetings. Turn off for "
                                           "songs, or singing may be missed."),
                "include_timestamps": self._switch(card, 2, "Add timestamps", "include_timestamps",
                                                   "e.g. [00:01:15 → 00:01:20] before each line."),
                "label_music": self._switch(card, 4, "Label music and songs", "label_music",
                                            "Adds [MUSIC] before song and jingle lines. "
                                            "Everything is still transcribed; dead air is "
                                            "marked [SILENCE]. Downloads 14 MB once."),
                "use_filename_hint": self._switch(card, 6, "Use file name as a hint",
                                                  "use_filename_hint",
                                                  "Helps spell names and song titles."),
            }
            ctk.CTkFrame(card, height=10, fg_color="transparent").grid(row=8, column=0)

            card = self._section(panel, "Vocabulary")
            ctk.CTkLabel(card, text="Names and fixes", anchor="w", font=font(12, "bold")
                         ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
            ghost_button(card, "Edit...", self.edit_vocabulary, width=80
                         ).grid(row=0, column=1, sticky="e", padx=12, pady=(10, 0))
            self.vocab_summary = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                              text_color=GRAY, font=font(11), wraplength=290)
            self.vocab_summary.grid(row=1, column=0, columnspan=2, sticky="w", padx=12,
                                    pady=(0, 4))
            ctk.CTkLabel(card, text="Jingles, ads, songs", anchor="w", font=font(12, "bold")
                         ).grid(row=2, column=0, sticky="w", padx=12, pady=(10, 0))
            ghost_button(card, "Manage...", self.manage_jingles, width=80
                         ).grid(row=2, column=1, sticky="e", padx=12, pady=(10, 0))
            self.jingle_summary = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                               text_color=GRAY, font=font(11), wraplength=290)
            self.jingle_summary.grid(row=3, column=0, columnspan=2, sticky="w", padx=12,
                                     pady=(0, 10))
            self.refresh_vocab_summary()

            card = self._section(panel, "Folders")
            self.folder_labels = {}
            for row, (key, title) in enumerate((("input_folder", "Inbox"),
                                                ("output_folder", "Transcripts"))):
                ctk.CTkLabel(card, text=title, anchor="w", font=font(12, "bold")
                             ).grid(row=row * 2, column=0, sticky="w", padx=12, pady=(10, 0))
                ghost_button(card, "Change...", lambda k=key: self.change_folder(k), width=80
                             ).grid(row=row * 2, column=1, sticky="e", padx=12, pady=(10, 0))
                lbl = ctk.CTkLabel(card, text="", text_color=GRAY, anchor="w", font=font(11))
                lbl.grid(row=row * 2 + 1, column=0, columnspan=2, sticky="w", padx=12)
                self.folder_labels[key] = lbl
            ctk.CTkFrame(card, height=10, fg_color="transparent").grid(row=4, column=0)

            card = self._section(panel, "Queue & system")
            self.order = self._field(card, 0, "Order", lambda p: Select(
                p, values=list(ORDER_CHOICES),
                command=lambda v: self.stage("order", ORDER_CHOICES[v])),
                "Which waiting file to do next.")
            self.device = self._field(card, 2, "Device", lambda p: Select(
                p, values=list(DEVICE_CHOICES),
                command=lambda v: self.stage("device", DEVICE_CHOICES[v])))
            self.autostart = self._switch(card, 4, "Start with Windows", None,
                                          "Starts hidden in the tray when you log in.",
                                          command=self.on_autostart)
            if exe_command() is None:
                self.autostart.configure(state="disabled")
            self.switches["check_for_updates"] = self._switch(
                card, 6, "Check for updates", "check_for_updates",
                "Once a day, shows a notice when a new version is out.")
            ctk.CTkFrame(card, height=10, fg_color="transparent").grid(row=8, column=0)

            row = ctk.CTkFrame(panel, fg_color="transparent")
            row.pack(fill="x", pady=(14, 4))
            ghost_button(row, "Reset to defaults", self.reset_settings, width=130).pack(side="left")
            self.saved_text = ctk.CTkLabel(panel, text="", text_color=GRAY, anchor="w",
                                           justify="left", wraplength=320)
            self.saved_text.pack(fill="x", padx=4)

        def _build_status_bar(self):
            bar = ctk.CTkFrame(self, fg_color="transparent", height=26)
            bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 8))
            self.bar_left = ctk.CTkLabel(bar, text="", text_color=GRAY, font=font(11))
            self.bar_left.pack(side="left")
            hint = "Tip: drag audio files onto this window to add them" if self.dnd else ""
            self.bar_right = ctk.CTkLabel(bar, text=hint, text_color=GRAY, font=font(11))
            self.bar_right.pack(side="right")

        # =================================================================
        # Settings
        # =================================================================

        def config_raw(self):
            try:
                raw = T.read_raw_config()
            except T.UserError:
                raw = {}
            merged = dict(T.DEFAULT_CONFIG)
            merged.update(raw)
            return merged

        def input_dir(self):
            return Path(self.config_raw()["input_folder"])

        def output_dir(self):
            return Path(self.config_raw()["output_folder"])

        def _set_menu(self, menu, choices, value):
            label = label_for(choices, value)
            values = list(choices) + ([label] if label not in choices else [])
            menu.set_values(values)
            menu.set(label)

        def current_value(self, key, config=None):
            """The applied (saved) value of a setting, normalised for comparing."""
            if key == "_autostart":
                return bool(get_autostart())
            c = config or self.config_raw()
            value = c.get(key)
            if key == "language":
                return str(value or "auto").strip().lower()
            if key in ("speed", "order", "device"):
                return str(value).strip().lower()
            if key in ("input_folder", "output_folder"):
                return str(Path(value))
            if key in ("vad_filter", "include_timestamps", "label_music", "use_filename_hint",
                       "check_for_updates"):
                return bool(value)
            return value

        def shown_values(self):
            """Saved settings with any not-yet-applied changes on top."""
            c = self.config_raw()
            keys = WINDOW_SETTINGS + ("input_folder", "output_folder", "_autostart")
            values = {k: self.current_value(k, c) for k in keys}
            values.update(self.pending)
            return values

        def load_settings_into_widgets(self):
            v = self.shown_values()
            self.mode.set(label_for(MODE_CHOICES, v["speed"]))
            self._set_menu(self.model, MODEL_CHOICES, v["model_size"])
            self._set_menu(self.language, LANGUAGE_CHOICES, v["language"])
            self._set_menu(self.order, ORDER_CHOICES, v["order"])
            self._set_menu(self.device, DEVICE_CHOICES, v["device"])
            for key, sw in self.switches.items():
                sw.select() if v[key] else sw.deselect()
            self.autostart.select() if v["_autostart"] else self.autostart.deselect()
            for key, lbl in self.folder_labels.items():
                if key in self.pending:
                    lbl.configure(text=short_path(v[key]) + "  (not applied yet)",
                                  text_color=AMBER)
                else:
                    lbl.configure(text=short_path(v[key]), text_color=GRAY)
            self.refresh_pending()

        # --- staging: nothing is saved until Apply -------------------------

        def stage(self, key, value):
            if value == self.current_value(key):
                self.pending.pop(key, None)  # changed back: nothing to apply
            else:
                self.pending[key] = value
            self.saved_text.configure(text="")
            if key in self.folder_labels:
                self.load_settings_into_widgets()
            else:
                self.refresh_pending()

        def refresh_pending(self):
            n = len(self.pending)
            if n:
                self.pending_text.configure(
                    text=f"\u25cf  {n} unsaved change{'s' if n > 1 else ''}", text_color=AMBER)
                self.apply_btn.configure(state="normal", fg_color=("#3B8ED0", "#1F6AA5"))
                self.discard_btn.configure(state="normal")
            else:
                if not self.just_applied:
                    self.pending_text.configure(text="All changes applied", text_color=GRAY)
                self.apply_btn.configure(state="disabled", fg_color=("gray75", "gray30"))
                self.discard_btn.configure(state="disabled")

        def discard_changes(self):
            self.pending.clear()
            self.saved_text.configure(text="")
            self.load_settings_into_widgets()

        def describe(self, key, value):
            maps = {"speed": MODE_CHOICES, "model_size": MODEL_CHOICES,
                    "language": LANGUAGE_CHOICES, "order": ORDER_CHOICES,
                    "device": DEVICE_CHOICES}
            if key in maps:
                return label_for(maps[key], value)
            if isinstance(value, bool):
                return "On" if value else "Off"
            if key in ("input_folder", "output_folder"):
                return short_path(value, 60)
            return str(value)

        def apply_changes(self):
            if not self.pending:
                return
            changes = dict(self.pending)
            lines = [f"\u2022  {SETTING_NAMES[k]}: {self.describe(k, self.current_value(k))}"
                     f"  \u2192  {self.describe(k, v)}" for k, v in changes.items()]

            # Explain what the bigger decisions will do, and ask first.
            notes = []
            busy = self.engine.snapshot()["file"]
            if "model_size" in changes:
                size = changes["model_size"]
                if model_downloaded(size):
                    notes.append("The new model is loaded (a few seconds).")
                else:
                    name = self.describe("model_size", size).split(" (")[0]
                    notes.append(f"The {name} model (about {MODEL_DOWNLOAD.get(size, '?')}) is "
                                 "downloaded first. Transcribing waits until it's ready; after "
                                 "that it works offline.")
            if changes.get("device") == "cuda":
                notes.append("If no NVIDIA GPU is set up, it falls back to the CPU.")
            if "input_folder" in changes:
                notes.append("Files in the old Inbox will no longer be watched.")
            if "output_folder" in changes:
                notes.append("New transcripts go to the new folder; existing ones stay where "
                             "they are.")
            if "_autostart" in changes:
                notes.append("The program will start hidden in the tray when you log in."
                             if changes["_autostart"] else
                             "The program will no longer start when you log in.")
            if busy:
                notes.append(f"{busy} finishes with the current settings first.")

            if any(k in changes for k in IMPORTANT_SETTINGS):
                message = "\n".join(lines) + ("\n\n" + "\n\n".join(notes) if notes else "")
                if not messagebox.askyesno("Apply these changes?", message, parent=self):
                    return

            config_changes = {k: v for k, v in changes.items() if not k.startswith("_")}
            try:
                if config_changes:
                    T.update_config(config_changes)
                if "_autostart" in changes:
                    set_autostart(changes["_autostart"])
            except Exception as e:
                messagebox.showerror("Could not save settings", str(e), parent=self)
                return
            for k, v in changes.items():
                log.info("Setting changed: %s = %s", SETTING_NAMES[k], self.describe(k, v))
            self.pending.clear()
            if config_changes:
                self.engine.settings_changed()
            if any(k in changes for k in ("input_folder", "output_folder")):
                self.completed, self.last_completed_scan = [], 0.0
                self.view_keys.clear()
            self.saved_text.configure(text="")
            self.just_applied = True
            self.load_settings_into_widgets()
            note = "Applied" + (" \u2013 from the next file" if busy else "")
            self.pending_text.configure(text=f"\u2713  {note}", text_color=GREEN)

            def settle():
                self.just_applied = False
                self.refresh_pending()
            self.after(5000, settle)

        def on_language(self, label):
            value = LANGUAGE_CHOICES.get(label)
            if value is None:  # "Other (xx)" from config.json
                value = label[label.find("(") + 1:-1]
            self.stage("language", value)

        def on_autostart(self):
            self.stage("_autostart", bool(self.autostart.get()))

        def reset_settings(self):
            for k in WINDOW_SETTINGS:
                self.stage(k, self.current_value(k, T.DEFAULT_CONFIG))
            self.load_settings_into_widgets()
            if self.pending:
                self.saved_text.configure(text="Defaults selected. Click Apply to use them, "
                                               "or Discard to keep your settings.",
                                          text_color=GRAY)

        def change_folder(self, key):
            current = self.shown_values()[key]
            title = ("Choose the Inbox folder (where audio files are dropped)"
                     if key == "input_folder" else
                     "Choose the Transcripts folder (where .txt files are saved)")
            chosen = filedialog.askdirectory(
                parent=self, title=title,
                initialdir=current if Path(current).is_dir() else str(Path.home()))
            if not chosen:
                return
            chosen = Path(chosen)
            other_key = "output_folder" if key == "input_folder" else "input_folder"
            other = Path(self.shown_values()[other_key])
            if chosen.resolve() == other.resolve():
                messagebox.showwarning("Same folder", "The Inbox and the Transcripts folder "
                                       "must be different folders.", parent=self)
                return
            self.stage(key, str(chosen))

        def edit_config(self):
            messagebox.showinfo(
                "Edit config.json",
                "config.json opens in Notepad. After saving your changes, click "
                "Try again (or restart the program) to use them.\n\n"
                "Most settings can be changed here in the window instead.", parent=self)
            try:
                subprocess.Popen(["notepad.exe", str(T.CONFIG_PATH)])
            except OSError as e:
                log.error("Could not open Notepad: %s", e)

        # =================================================================
        # Files
        # =================================================================

        def add_files(self):
            types = " ".join("*" + ext for ext in T.DEFAULT_CONFIG["file_types"])
            files = filedialog.askopenfilenames(parent=self, title="Add audio or video files",
                                                filetypes=[("Audio and video", types),
                                                           ("All files", "*.*")])
            self.copy_to_inbox(files)

        def on_drop(self, event):
            self.copy_to_inbox(self.tk.splitlist(event.data))

        def copy_to_inbox(self, files):
            """Copy files into the Inbox in the background (big files take a while).
            The engine waits for each copy to finish before starting it."""
            file_types = {e.lower() for e in self.config_raw()["file_types"]}
            paths = [Path(f) for f in files]
            good = [p for p in paths if p.is_file() and p.suffix.lower() in file_types]
            ignored = [p.name for p in paths if p not in good]
            if ignored:
                messagebox.showwarning(
                    "Some files were not added",
                    "These are not supported audio or video files (or are folders):\n\n" +
                    "\n".join(shorten(n, 60) for n in ignored[:10]) +
                    ("\n..." if len(ignored) > 10 else ""), parent=self)
            if not good:
                return
            inbox = self.input_dir()
            self.copying += len(good)

            def work():
                for src in good:
                    dest = inbox / src.name
                    try:
                        if src.resolve().parent == inbox.resolve():
                            log.info("Already in the Inbox: %s", src.name)
                        elif dest.exists():
                            log.warning("Not added – a file named %s is already in the Inbox",
                                        src.name)
                        else:
                            shutil.copy2(src, dest)
                            log.info("Added to the Inbox: %s", src.name)
                    except OSError as e:
                        log.error("Could not add %s: %s", src.name, e)
                    finally:
                        self.ui_calls.put(self._copy_finished)
                self.engine.wake()

            threading.Thread(target=work, daemon=True).start()

        def _copy_finished(self):
            self.copying = max(0, self.copying - 1)

        # =================================================================
        # Actions
        # =================================================================

        def toggle_pause(self):
            self.engine.set_paused(not self.engine.paused)
            if self.tray:
                self.tray.update_menu()

        def skip_file(self):
            name = self.engine.snapshot()["file"]
            if not name:
                return
            if messagebox.askyesno(
                    "Skip this file?",
                    f"Skip {name}?\n\nProgress so far will be lost. The file stays in the "
                    "Inbox; you can retry it from the Issues tab.", icon="warning", parent=self):
                if self.engine.snapshot()["file"] == name:  # still the same file
                    self.engine.skip_current()

        def retry_all(self):
            if self.engine.snapshot()["issues"]:
                self.engine.retry()
                log.info("Retrying all files in Issues")

        # --- updates ---------------------------------------------------------

        def open_url(self, url):
            if url:
                import webbrowser
                webbrowser.open(url)

        def schedule_update_check(self):
            """Check now (in the background) and again every 24 hours."""
            if self.config_raw().get("check_for_updates", True):
                self.check_updates_background()
            self.after(UPDATE_CHECK_EVERY * 1000, self.schedule_update_check)

        def check_updates_background(self, manual=False):
            def work():
                try:
                    info, error = fetch_latest_release(), None
                except Exception as e:
                    info, error = None, str(e)
                self.ui_calls.put(lambda: self.on_update_result(info, error, manual))
            threading.Thread(target=work, name="update-check", daemon=True).start()

        def check_updates_now(self):
            self.check_updates_background(manual=True)

        def on_update_result(self, info, error, manual):
            newer = bool(info and version_tuple(info["version"]) > version_tuple(T.__version__))
            if error:
                log.info("Update check: %s", error)
            if newer:
                self.update_info = info
                dismissed = self.config_raw().get("dismissed_update")
                if manual or dismissed != info["version"]:
                    self.show_banner()
                if not manual:
                    log.info("Version %s is available", info["version"])
            if manual:
                if error:
                    messagebox.showwarning("Check for updates", error, parent=self)
                elif newer:
                    messagebox.showinfo("Update available",
                                        f"Version {info['version']} is available (you have "
                                        f"{T.__version__}).\n\nClick Download in the blue "
                                        "notice to get it.", parent=self)
                else:
                    messagebox.showinfo("Check for updates",
                                        f"You have the latest version ({T.__version__}).",
                                        parent=self)

        def show_banner(self):
            info = self.update_info
            if self.update_state:
                return  # already updating
            self.set_banner(f"Version {info['version']} is available",
                            f"You have {T.__version__}")
            one_click = bool(info.get("sha256")) and can_self_update()
            self.update_btn.configure(text="Update now" if one_click else "Download", command=self.start_update,
                                      state="normal")
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", pady=(0, 12), before=self.status_card)

        def set_banner(self, title, detail=""):
            self.banner_text.configure(text=title)
            self.banner_sub.configure(text=detail)

        # ---------------------------------------------------------- vocabulary
        # ------------------------------------------------------ transcript review
        def review_transcript(self, path):
            """Show a transcript; select a wrong word, right-click, correct it."""
            path = Path(path)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                messagebox.showerror("Open transcript", f"Couldn't open:\n{e}", parent=self)
                return
            win = ctk.CTkToplevel(self)
            win.title(path.name)
            win.geometry("860x640")
            win.transient(self)
            win.grid_columnconfigure(0, weight=1)
            win.grid_rowconfigure(1, weight=1)

            top = ctk.CTkFrame(win, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
            READ_HINT = ("Wrong word? Double-click it, then right-click → Correct.  "
                         "Bigger changes: Edit.")
            EDIT_HINT = ("Editing: type, delete, or press Enter to split a line. "
                         "Click Save when done.")
            hint = ctk.CTkLabel(top, text=READ_HINT, text_color=GRAY, font=font(12), anchor="w")
            hint.pack(side="left")
            # Reading mode buttons
            read_bar = ctk.CTkFrame(top, fg_color="transparent")
            read_bar.pack(side="right")
            ghost_button(read_bar, "Show in folder", lambda: show_in_folder(path), width=104
                         ).pack(side="right")
            ghost_button(read_bar, "Open in Notepad", lambda: open_path(path), width=116
                         ).pack(side="right", padx=8)
            # Editing mode buttons
            edit_bar = ctk.CTkFrame(top, fg_color="transparent")

            box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=13), wrap="word")
            box.grid(row=1, column=0, sticky="nsew", padx=16)
            box.insert("1.0", text)
            box.configure(state="disabled")  # read-only; selecting still works
            tk_text = box._textbox
            tk_text.tag_configure("fixed", background="#2e6b3f")

            status = ctk.CTkLabel(win, text="", text_color=GRAY, font=font(11), anchor="w")
            status.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 12))

            def selected_word(event=None):
                try:
                    return tk_text.get("sel.first", "sel.last").strip()
                except Exception:
                    pass
                if event is None:
                    return ""
                at = f"@{event.x},{event.y}"
                start, end = tk_text.index(f"{at} wordstart"), tk_text.index(f"{at} wordend")
                tk_text.tag_add("sel", start, end)
                return tk_text.get(start, end).strip()

            def reload(highlight=None):
                new_text = path.read_text(encoding="utf-8")
                y = tk_text.yview()[0]
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.insert("1.0", new_text)
                if highlight:
                    start = "1.0"
                    while True:
                        pos = tk_text.search(highlight, start, stopindex="end", nocase=True)
                        if not pos:
                            break
                        end = f"{pos}+{len(highlight)}c"
                        tk_text.tag_add("fixed", pos, end)
                        start = end
                box.configure(state="disabled")
                tk_text.yview_moveto(y)

            # ------------------------------------------------------- editing
            editing = {"on": False, "original": ""}

            def current_text():
                return tk_text.get("1.0", "end-1c")

            def start_edit():
                editing.update(on=True, original=current_text())
                tk_text.tag_remove("fixed", "1.0", "end")
                box.configure(state="normal")
                tk_text.configure(undo=True, autoseparators=True, maxundo=-1)
                tk_text.edit_reset()
                read_bar.pack_forget()
                edit_bar.pack(side="right")
                hint.configure(text=EDIT_HINT)
                status.configure(text="")
                tk_text.focus_set()

            def stop_edit():
                editing["on"] = False
                box.configure(state="disabled")
                edit_bar.pack_forget()
                read_bar.pack(side="right")
                hint.configure(text=READ_HINT)

            def save_edit():
                new_text = current_text().rstrip("\n") + "\n"
                tmp = path.with_name(path.name + f".{T.HOSTNAME}.edit")
                try:
                    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                        f.write(new_text)
                    os.replace(tmp, path)
                except OSError as e:
                    messagebox.showerror("Save transcript", f"Couldn't save:\n{e}", parent=win)
                    return False
                log.info("Transcript edited: %s", path.name)
                stop_edit()
                status.configure(text=f"Saved  ·  {datetime.now():%H:%M}")
                self.last_completed_scan = 0.0  # refresh word counts
                return True

            def cancel_edit():
                if current_text() != editing["original"] and not messagebox.askyesno(
                        "Discard changes?", "Throw away the changes you made?", parent=win):
                    return
                stop_edit()
                reload()
                status.configure(text="Changes discarded.")

            def close_window(event=None):
                if editing["on"] and current_text() != editing["original"]:
                    answer = messagebox.askyesnocancel(
                        "Save changes?", "Save the changes to this transcript?", parent=win)
                    if answer is None or (answer and not save_edit()):
                        return
                win.destroy()

            edit_btn = ctk.CTkButton(read_bar, text="Edit", width=70, height=28,
                                     command=start_edit)
            edit_btn.pack(side="right", padx=(0, 8))
            ghost_button(edit_bar, "Cancel", cancel_edit, width=80, height=28).pack(side="right")
            ctk.CTkButton(edit_bar, text="Save", width=80, height=28, command=save_edit
                          ).pack(side="right", padx=(0, 8))

            def correct(event=None):
                if editing["on"]:
                    return
                wrong = selected_word(event)
                if not wrong or len(wrong) > 60 or "\n" in wrong:
                    status.configure(text="Select a word or a few words first.")
                    return
                self.ask_correction(win, path, wrong, status, reload)

            menu = tk.Menu(win, tearoff=0)

            def popup(event):
                menu.delete(0, "end")
                if editing["on"]:
                    for label, ev in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"),
                                      ("Paste", "<<Paste>>")):
                        menu.add_command(label=label,
                                         command=lambda e=ev: tk_text.event_generate(e))
                    menu.add_separator()
                    menu.add_command(label="Undo", command=lambda: tk_text.event_generate("<<Undo>>"))
                    menu.tk_popup(event.x_root, event.y_root)
                    return
                word = selected_word(event)
                if word and len(word) <= 60 and "\n" not in word:
                    menu.add_command(label=f'Correct "{shorten(word, 30)}"...',
                                     command=lambda: self.ask_correction(
                                         win, path, word, status, reload))
                else:
                    menu.add_command(label="Select a word first", state="disabled")
                menu.add_separator()
                menu.add_command(label="Save as jingle / ad / song...",
                                 command=lambda: self.save_as_jingle(win, path, tk_text, status,
                                                                     reload))
                menu.add_separator()
                menu.add_command(label="Copy", command=lambda: tk_text.event_generate("<<Copy>>"))
                menu.tk_popup(event.x_root, event.y_root)

            tk_text.bind("<Button-3>", popup)
            win.bind("<Control-e>", correct)
            win.bind("<Control-s>", lambda e: save_edit() if editing["on"] else None)
            win.bind("<Escape>", lambda e: cancel_edit() if editing["on"] else close_window())
            win.protocol("WM_DELETE_WINDOW", close_window)
            win.after(100, win.focus_force)
            win._start_edit, win._save_edit, win._text = start_edit, save_edit, tk_text  # tests

        # ------------------------------------------------------------- jingles
        TIME_RE = re.compile(r"\[(?:[A-Z]+ )?(\d\d:\d\d:\d\d) → (\d\d:\d\d:\d\d)\]")

        @staticmethod
        def _seconds(stamp):
            h, m, sec = (int(x) for x in stamp.split(":"))
            return h * 3600 + m * 60 + sec

        def save_as_jingle(self, parent, path, tk_text, status, reload):
            """Selected lines -> a jingle: name, correct text, start/end."""
            try:
                first = tk_text.index("sel.first linestart")
                last = tk_text.index("sel.last lineend")
                selected = tk_text.get(first, last)
            except Exception:
                selected = ""
            lines = [l for l in selected.splitlines() if l.strip()]
            stamps = [m for l in lines if (m := self.TIME_RE.match(l))]
            if not stamps:
                messagebox.showinfo(
                    "Save as jingle",
                    "Select the jingle's lines first (they need timestamps: turn on "
                    "Add timestamps in Settings), then right-click → Save as jingle.",
                    parent=parent)
                return
            source = self.source_of.get(path.name.lower()) or self.guess_source(path)
            audio_path = (self.engine.inbox / source) if (source and self.engine.inbox) else None
            if not audio_path or not audio_path.exists():
                messagebox.showerror(
                    "Save as jingle",
                    "The original recording for this transcript isn't in the Inbox any "
                    "more, so its sound can't be saved.", parent=parent)
                return
            # Keep each line's time so songs can be saved line by line; drop
            # labels like MUSIC and lines that are only "(instrumental)".
            prefill = []
            for l in lines:
                m = self.TIME_RE.match(l)
                body = l.split("] ", 1)[-1] if m else l
                if body.startswith("(") and body.endswith(")"):
                    continue
                prefill.append(f"[{m.group(1)} → {m.group(2)}] {body}" if m else body)
            text = "\n".join(prefill)

            dlg = ctk.CTkToplevel(parent)
            dlg.title("Save as jingle / ad / song")
            dlg.geometry("640x560")
            dlg.transient(parent)
            dlg.after(100, dlg.grab_set)
            dlg.grid_columnconfigure((0, 1), weight=1)
            ctk.CTkLabel(dlg, text="Name", font=font(12, "bold"), anchor="w"
                         ).grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(14, 2))
            name = ctk.CTkEntry(dlg, placeholder_text="e.g. Love Radio – Tuloy ang ligaya")
            name.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18)
            ctk.CTkLabel(dlg, text="Starts at", font=font(12, "bold"), anchor="w"
                         ).grid(row=2, column=0, sticky="w", padx=(18, 6), pady=(10, 2))
            ctk.CTkLabel(dlg, text="Ends at", font=font(12, "bold"), anchor="w"
                         ).grid(row=2, column=1, sticky="w", padx=(6, 18), pady=(10, 2))
            start_e = ctk.CTkEntry(dlg)
            start_e.insert(0, stamps[0].group(1))
            start_e.grid(row=3, column=0, sticky="ew", padx=(18, 6))
            end_e = ctk.CTkEntry(dlg)
            end_e.insert(0, stamps[-1].group(2))
            end_e.grid(row=3, column=1, sticky="ew", padx=(6, 18))
            kind_box = ctk.CTkFrame(dlg, fg_color="transparent")
            kind_box.grid(row=4, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 0))
            ctk.CTkLabel(kind_box, text="Type", font=font(12, "bold")).pack(side="left")
            kind = ctk.CTkSegmentedButton(kind_box, values=["Jingle", "Ad", "Song"])
            kind.set("Song" if (self._seconds(stamps[-1].group(2))
                                - self._seconds(stamps[0].group(1)) > 90) else "Jingle")
            kind.pack(side="left", padx=12)
            ctk.CTkLabel(dlg, text="Correct text (written every time it plays)",
                         font=font(12, "bold"), anchor="w"
                         ).grid(row=5, column=0, columnspan=2, sticky="w", padx=18, pady=(10, 0))
            ctk.CTkLabel(dlg, text="Fix the words after each [time]. Keep the times to save "
                                   "it line by line (best for songs), or replace everything "
                                   "with one paragraph.",
                         text_color=GRAY, font=font(11), anchor="w", justify="left",
                         wraplength=600).grid(row=6, column=0, columnspan=2, sticky="w", padx=18)
            text_box = ctk.CTkTextbox(dlg, height=200, wrap="word")
            text_box.insert("1.0", text)
            text_box.grid(row=7, column=0, columnspan=2, sticky="nsew", padx=18, pady=(4, 0))
            dlg.grid_rowconfigure(7, weight=1)
            bar = ctk.CTkFrame(dlg, fg_color="transparent")
            bar.grid(row=8, column=0, columnspan=2, sticky="e", padx=18, pady=14)

            def save():
                try:
                    a = self._seconds(start_e.get().strip())
                    b = self._seconds(end_e.get().strip())
                except ValueError:
                    messagebox.showwarning("Save as jingle", "Times must look like 00:01:23.",
                                           parent=dlg)
                    return
                label = name.get().strip() or kind.get()
                raw_lines = [l.strip() for l in text_box.get("1.0", "end").splitlines()
                             if l.strip()]
                timed = []
                for l in raw_lines:
                    m = LINE_TIME.match(l)
                    if m and m.group(3).strip():
                        timed.append((self._seconds(m.group(1)) - a,
                                      self._seconds(m.group(2)) - a, m.group(3).strip()))
                    elif timed and not m:  # untimed line: belongs to the line above
                        ta, tb, tt = timed[-1]
                        timed[-1] = (ta, tb, f"{tt} {l}")
                correct = " ".join(ANY_TIME.sub(" ", " ".join(raw_lines)).split())
                if not timed or len(timed) < len(raw_lines) // 2:
                    timed = None  # written as one paragraph
                if not correct:
                    messagebox.showwarning("Save as jingle", "Type the correct text first.",
                                           parent=dlg)
                    return
                dlg.configure(cursor="watch")
                dlg.update()
                try:
                    import jingles
                    from faster_whisper.audio import decode_audio
                    audio = decode_audio(str(audio_path))
                    jingles.save_jingle(self.output_dir(), label, correct, audio, a, b,
                                        kind=kind.get().lower(), lines=timed)
                except Exception as e:
                    dlg.configure(cursor="")
                    messagebox.showerror("Save as jingle", str(e), parent=dlg)
                    return
                log.info('Jingle saved: "%s" (%s, %d s)', label, audio_path.name, b - a)
                dlg.destroy()
                self.refresh_vocab_summary()
                # Show it in this transcript straight away.
                if messagebox.askyesno(
                        "Jingle saved",
                        f'"{label}" will be recognised in every new file.\n\n'
                        "Replace the selected lines in this transcript with it now?",
                        parent=parent):
                    self.replace_with_jingle(path, a, b, correct, kind.get().upper(), timed)
                    reload(highlight=correct[:30])
                status.configure(text=f'Jingle "{label}" saved')

            ghost_button(bar, "Cancel", dlg.destroy, width=80, height=30).pack(side="right")
            ctk.CTkButton(bar, text="Save", width=90, height=30, command=save
                          ).pack(side="right", padx=(0, 8))
            dlg.bind("<Escape>", lambda e: dlg.destroy())

        def replace_with_jingle(self, path, a, b, text, label="JINGLE", timed=None):
            with open(path, encoding="utf-8") as f:
                old = f.read().split("\n")
            new, placed = [], False
            if timed:
                line = "\n".join(f"[{label} {T.format_timestamp(a + ta)} → "
                                  f"{T.format_timestamp(a + tb)}] {tt}" for ta, tb, tt in timed)
            else:
                line = f"[{label} {T.format_timestamp(a)} → {T.format_timestamp(b)}] {text}"
            for l in old:
                m = self.TIME_RE.match(l)
                inside = m and a <= self._seconds(m.group(1)) < b and not l.startswith("[SILENCE")
                if inside:
                    if not placed:
                        new.append(line)
                        placed = True
                    continue
                if not placed and m and self._seconds(m.group(1)) >= b:
                    new.append(line)
                    placed = True
                new.append(l)
            if not placed:
                new.append(line)
            tmp = path.with_name(path.name + f".{T.HOSTNAME}.jingle")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(new))
            os.replace(tmp, path)

        def manage_jingles(self):
            import jingles
            win = ctk.CTkToplevel(self)
            win.title("Jingles, ads and songs")
            win.geometry("620x480")
            win.transient(self)
            ctk.CTkLabel(win, text="Saved jingles, ads and songs", font=font(14, "bold"), anchor="w"
                         ).pack(fill="x", padx=18, pady=(14, 0))
            ctk.CTkLabel(win, text="Shared by every PC that uses this Transcripts folder. "
                                   "To add one: open a transcript, select the jingle's lines, "
                                   "right-click → Save as jingle.",
                         text_color=GRAY, font=font(11), anchor="w", justify="left",
                         wraplength=580).pack(fill="x", padx=18, pady=(0, 8))
            body = ctk.CTkScrollableFrame(win)
            body.pack(fill="both", expand=True, padx=18, pady=(0, 14))

            def fill():
                for w in body.winfo_children():
                    w.destroy()
                items = jingles.load_library(self.output_dir())
                if not items:
                    ctk.CTkLabel(body, text="None saved yet.", text_color=GRAY).pack(pady=20)
                for j in items:
                    row = ctk.CTkFrame(body, corner_radius=8)
                    row.pack(fill="x", pady=4)
                    info = ctk.CTkFrame(row, fg_color="transparent")
                    info.pack(side="left", fill="x", expand=True, padx=12, pady=8)
                    kind_label = {"song": "Song", "ad": "Ad"}.get(j.get("kind"), "Jingle")
                    n_lines = len(j.get("lines") or [])
                    extra = f"  ·  {n_lines} lines" if n_lines else ""
                    ctk.CTkLabel(info, text=f"{j['name']}  ·  {kind_label}  ·  "
                                            f"{j.get('seconds', 0):.0f} s{extra}",
                                 font=font(12, "bold"), anchor="w").pack(fill="x")
                    ctk.CTkLabel(info, text=shorten(j.get("text", ""), 90), text_color=GRAY,
                                 font=font(11), anchor="w").pack(fill="x")

                    def remove(jid=j["id"], label=j["name"]):
                        if messagebox.askyesno("Delete jingle",
                                               f'Delete "{label}"? It won\'t be recognised '
                                               "in new files any more.", parent=win):
                            jingles.delete_jingle(self.output_dir(), jid)
                            fill()
                            self.refresh_vocab_summary()
                    ghost_button(row, "Delete", remove, width=70).pack(side="right", padx=12)
            fill()
            win.bind("<Escape>", lambda e: win.destroy())

        def ask_correction(self, parent, path, wrong, status, reload):
            """'Correct "Tuyoy" to: [Tuloy]', only here or always."""
            dlg = ctk.CTkToplevel(parent)
            dlg.title("Correct")
            dlg.geometry("440x250")
            dlg.resizable(False, False)
            dlg.transient(parent)
            dlg.after(100, dlg.grab_set)
            ctk.CTkLabel(dlg, text=f'Correct "{shorten(wrong, 40)}" to:', font=font(13, "bold"),
                         anchor="w").pack(fill="x", padx=18, pady=(16, 6))
            entry = ctk.CTkEntry(dlg, height=34)
            entry.insert(0, wrong)
            entry.pack(fill="x", padx=18)
            entry.after(150, lambda: (entry.focus_set(), entry.select_range(0, "end")))
            scope = ctk.StringVar(value="always")
            ctk.CTkRadioButton(dlg, text="Always (this and future transcripts)", variable=scope,
                               value="always").pack(anchor="w", padx=18, pady=(14, 4))
            ctk.CTkRadioButton(dlg, text="Only in this transcript", variable=scope,
                               value="here").pack(anchor="w", padx=18)
            bar = ctk.CTkFrame(dlg, fg_color="transparent")
            bar.pack(fill="x", padx=18, pady=16, side="bottom")

            def ok(event=None):
                right = entry.get().strip()
                if not right or right == wrong:
                    dlg.destroy()
                    return
                always = scope.get() == "always"
                dlg.destroy()
                self.apply_correction(parent, path, wrong, right, always, status, reload)

            ghost_button(bar, "Cancel", dlg.destroy, width=80, height=30).pack(side="right")
            ctk.CTkButton(bar, text="Correct", width=90, height=30, command=ok
                          ).pack(side="right", padx=(0, 8))
            dlg.bind("<Return>", ok)
            dlg.bind("<Escape>", lambda e: dlg.destroy())

        def apply_correction(self, parent, path, wrong, right, always, status, reload):
            fix = T.make_replacer({wrong: right})
            try:
                here = fix_transcript_file(path, fix)
            except OSError as e:
                messagebox.showerror("Correct", f"Couldn't save the transcript:\n{e}",
                                     parent=parent)
                return
            parts = [f'"{wrong}" → "{right}": {here} line{"s" if here != 1 else ""} fixed here']
            if always:
                try:
                    raw = self.config_raw()
                    fixes = dict(raw.get("replacements") or {})
                    fixes[wrong] = right
                    T.update_config({"replacements": fixes})
                    self.engine.settings_changed()
                    self.refresh_vocab_summary()
                    parts.append("added to Fixes for future transcripts")
                    log.info('Fix added: "%s" -> "%s"', wrong, right)
                except Exception as e:
                    messagebox.showerror("Correct", f"Couldn't save the fix:\n{e}", parent=parent)
                # Older transcripts with the same mistake.
                others = []
                try:
                    for other in self.output_dir().glob("*.txt"):
                        if other != path and not other.name.startswith("."):
                            n = count_in_transcript(other, fix)
                            if n:
                                others.append((other, n))
                except OSError:
                    pass
                if others:
                    lines = sum(n for _, n in others)
                    if messagebox.askyesno(
                            "Fix older transcripts too?",
                            f'"{wrong}" also appears in {len(others)} other transcript'
                            f'{"s" if len(others) != 1 else ""} ({lines} line'
                            f'{"s" if lines != 1 else ""}).\n\nCorrect those too?',
                            parent=parent):
                        done = 0
                        for other, _ in others:
                            try:
                                done += 1 if fix_transcript_file(other, fix) else 0
                            except OSError:
                                pass
                        parts.append(f"{done} other transcript{'s' if done != 1 else ''} fixed")
            status.configure(text="  ·  ".join(parts))
            reload(highlight=right)

        def refresh_vocab_summary(self):
            c = self.config_raw()
            words = c.get("word_list") or []
            if isinstance(words, str):
                words = [w for w in words.replace("\n", ",").split(",") if w.strip()]
            fixes = c.get("replacements") or {}
            if not words and not fixes:
                text = ("Spell station, host, sponsor and place names right, and fix "
                        "mistakes that keep coming back.")
            else:
                text = (f"{len(words)} name{'s' if len(words) != 1 else ''}  ·  "
                        f"{len(fixes)} fix{'es' if len(fixes) != 1 else ''}")
            self.vocab_summary.configure(text=text)
            try:
                import jingles
                count = len(jingles.load_library(self.output_dir()))
            except Exception:
                count = 0
            self.jingle_summary.configure(
                text=(f"{count} saved. Recognised in every new file and written exactly as "
                      "saved." if count else
                      "Correct a jingle or ad once (transcript window: select its lines, "
                      "right-click → Save as jingle) and it's recognised from then on."))

        def edit_vocabulary(self):
            """A window with two lists: names to spell right, and fixes."""
            c = self.config_raw()
            words = c.get("word_list") or []
            if isinstance(words, str):
                words = [w.strip() for w in words.replace("\n", ",").split(",") if w.strip()]
            fixes = c.get("replacements") or {}

            win = ctk.CTkToplevel(self)
            win.title("Vocabulary")
            win.geometry("620x560")
            win.transient(self)
            win.after(100, win.grab_set)
            win.grid_columnconfigure((0, 1), weight=1)
            win.grid_rowconfigure(2, weight=1)

            ctk.CTkLabel(win, text="Names to spell right", font=font(14, "bold"), anchor="w"
                         ).grid(row=0, column=0, sticky="w", padx=(18, 8), pady=(16, 0))
            ctk.CTkLabel(win, text="One per line: stations, hosts, sponsors, places.\n"
                                   "Used as hints, so keep it to real names.",
                         text_color=GRAY, font=font(11), anchor="w", justify="left"
                         ).grid(row=1, column=0, sticky="w", padx=(18, 8))
            word_box = ctk.CTkTextbox(win, font=font(12))
            word_box.grid(row=2, column=0, sticky="nsew", padx=(18, 8), pady=8)
            word_box.insert("1.0", "\n".join(words))

            ctk.CTkLabel(win, text="Fixes", font=font(14, "bold"), anchor="w"
                         ).grid(row=0, column=1, sticky="w", padx=(8, 18), pady=(16, 0))
            ctk.CTkLabel(win, text="One per line:  wrong = right\n"
                                   "Whole words, any capitals. e.g.  Love rate = Love Radio",
                         text_color=GRAY, font=font(11), anchor="w", justify="left"
                         ).grid(row=1, column=1, sticky="w", padx=(8, 18))
            fix_box = ctk.CTkTextbox(win, font=font(12))
            fix_box.grid(row=2, column=1, sticky="nsew", padx=(8, 18), pady=8)
            fix_box.insert("1.0", "\n".join(f"{k} = {v}" for k, v in fixes.items()))

            note = ctk.CTkLabel(win, text="Applies to files transcribed from now on.",
                                text_color=GRAY, font=font(11), anchor="w")
            note.grid(row=3, column=0, sticky="w", padx=18)
            bar = ctk.CTkFrame(win, fg_color="transparent")
            bar.grid(row=3, column=1, sticky="e", padx=18, pady=14)

            def save():
                new_words = [w.strip() for w in word_box.get("1.0", "end").splitlines()
                             if w.strip()]
                new_fixes, bad = {}, []
                for line in fix_box.get("1.0", "end").splitlines():
                    if not line.strip():
                        continue
                    if "=" not in line:
                        bad.append(line.strip())
                        continue
                    wrong, right = line.split("=", 1)
                    if wrong.strip():
                        new_fixes[wrong.strip()] = right.strip()
                if bad:
                    messagebox.showwarning(
                        "Vocabulary", "These fixes need an = sign (wrong = right):\n\n"
                        + "\n".join(bad[:8]), parent=win)
                    return
                try:
                    T.update_config({"word_list": new_words, "replacements": new_fixes})
                except Exception as e:
                    messagebox.showerror("Vocabulary", f"Couldn't save:\n{e}", parent=win)
                    return
                log.info("Vocabulary saved: %d names, %d fixes", len(new_words), len(new_fixes))
                self.engine.settings_changed()
                self.refresh_vocab_summary()
                win.destroy()

            ghost_button(bar, "Cancel", win.destroy, width=80, height=30).pack(side="right")
            ctk.CTkButton(bar, text="Save", width=90, height=30, command=save
                          ).pack(side="right", padx=(0, 8))
            win.bind("<Escape>", lambda e: win.destroy())

        def start_update(self):
            info = self.update_info or {}
            if not (info.get("sha256") and can_self_update()):
                self.open_url(info.get("download"))  # zip / source: download page
                return
            if self.update_state:
                return
            self.update_state = "downloading"
            self.update_btn.configure(state="disabled", text="Downloading...")
            self.set_banner(f"Downloading version {info['version']}...",
                            "You can keep working.")
            log.info("Downloading update %s", info["version"])

            def progress(fraction):
                self.ui_calls.put(lambda: self.banner_text.configure(
                    text=f"Downloading version {info['version']}...  {fraction:.0%}"))

            def work():
                try:
                    path, error = download_update(info, progress), None
                except Exception as e:
                    path, error = None, str(e)
                self.ui_calls.put(lambda: self.on_update_downloaded(path, error))
            threading.Thread(target=work, name="update-download", daemon=True).start()

        def on_update_downloaded(self, path, error):
            if error:
                log.warning("Update failed: %s", error)
                self.update_state = None
                self.show_banner()
                self.update_btn.configure(text="Try again")
                self.set_banner("Update failed", shorten(error, 90))
                return
            log.info("Update downloaded and verified: %s", path)
            self.update_path = path
            self.update_state = "waiting"
            self.install_when_idle()

        def install_when_idle(self):
            """Never interrupt a file: wait until the current one is done."""
            if self.quitting or self.update_state != "waiting":
                return
            busy = self.engine.snapshot()["file"]
            if busy:
                self.set_banner("Update ready",
                                f"It installs when {shorten(busy, 50)} is done.")
                self.update_btn.configure(text="Install now", state="normal",
                                          command=self.install_update_now)
                self.after(2000, self.install_when_idle)
                return
            self.install_update_now()

        def install_update_now(self):
            busy = self.engine.snapshot()["file"]
            if busy and not messagebox.askyesno(
                    "Install the update now?",
                    f"{busy} is still being transcribed. If you update now, it starts "
                    "again from the beginning after the update.", icon="warning",
                    parent=self):
                return
            self.update_state = "installing"
            self.update_btn.configure(state="disabled", text="Installing...")
            self.set_banner("Installing the update...",
                            "The program reopens in a few seconds.")
            self.update_idletasks()
            log.info("Installing update from %s", self.update_path)
            try:
                # Detached, so the installer keeps running after we quit.
                subprocess.Popen(
                    [str(self.update_path), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                     "/NORESTART", "/RELAUNCH"],
                    creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
                    close_fds=True)
            except OSError as e:
                self.update_state = None
                self.show_banner()
                self.set_banner("Couldn't start the installer", shorten(str(e), 90))
                return
            self.after(500, lambda: self.quit_app(force=True))

        def dismiss_update(self):
            """Hide the notice until an even newer version comes out."""
            if self.update_state == "installing":
                return
            self.update_state = None
            if self.update_info:
                try:
                    T.update_config({"dismissed_update": self.update_info["version"]})
                except Exception:
                    pass
            self.banner.pack_forget()

        def show_about(self):
            messagebox.showinfo(
                f"About {T.APP_NAME}",
                f"{T.APP_NAME} {T.__version__}\n\n"
                "Turns audio and video files into text, offline, on this PC.\n"
                "Speech recognition: faster-whisper (OpenAI Whisper models).\n\n"
                f"This PC: {T.HOSTNAME}\n"
                f"Program folder: {T.APP_DIR}\n\n"
                f"{PROJECT_URL}\n"
                "MIT License", parent=self)

        def open_menu(self):
            # Right-align the menu under the button.
            x = (self.menu_btn.winfo_rootx() + self.menu_btn.winfo_width()
                 - self.menu.winfo_reqwidth())
            y = self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height() + 2
            try:
                self.menu.tk_popup(x, y)
            finally:
                self.menu.grab_release()

        # =================================================================
        # Refreshing
        # =================================================================

        def _click_anywhere(self, event):
            """Close an open dropdown list when clicking somewhere else."""
            for select in list(open_popup):
                if not select.owns(event.widget):
                    select.close()

        def tick(self):
            """Runs 5 times a second: update everything from the engine."""
            if open_popup and self.focus_displayof() is None:
                close_popups()  # switched to another program
            while not self.ui_calls.empty():
                try:
                    self.ui_calls.get_nowait()()
                except Exception:
                    T.file_only.error("UI error:\n%s", T.traceback.format_exc())
            self.drain_log()
            while not self.engine.events.empty():
                self.on_engine_event(*self.engine.events.get_nowait())

            s = self.engine.snapshot()
            self.refresh_status(s)
            self.refresh_tab_labels(s)
            self.refresh_tabs(s)
            if not self.quitting:
                self.after(200, self.tick)

        def refresh_status(self, s):
            phase = s["phase"]
            title, colour = PHASES.get(phase, (phase, GRAY))
            self.pill.configure(text=f"  {title}  ", fg_color=colour)
            usable = phase not in ("error", "stopping", "loading", "starting")
            self.pause_btn.configure(
                text="Resume" if self.engine.paused else "Pause",
                state="normal" if usable else "disabled",
                fg_color=("#3B8ED0", "#1F6AA5") if usable else ("gray75", "gray30"))

            waiting = len(s["queue"])
            waiting_text = f"{waiting} waiting"
            if waiting and self.engine.inbox:
                infos = [self.details.get(self.engine.inbox / q[0]) for q in s["queue"][:500]]
                total = sum((i or {}).get("duration", 0) for i in infos)
                if total >= 60:
                    waiting_text += f" ({format_duration(total)} of audio)"
            parts = [waiting_text, f"{self.done_today()} done today"]
            if s["issues"]:
                parts.append(f"{len(s['issues'])} with issues")
            self.counts_text.configure(text="  ·  ".join(parts))

            # Buttons that only make sense in some states.
            self._show(self.skip_btn, bool(s["file"]) and phase != "stopping",
                       side="right", padx=(0, 8))
            self._show(self.retry_btn, phase == "error", side="right", padx=(0, 8))

            # The bar only shows while something is happening.
            bar_visible = bool(s["file"]) or phase in ("loading", "starting", "stopping")
            if bar_visible and not self.progress.winfo_ismapped():
                self.progress.grid()
            elif not bar_visible and self.progress.winfo_ismapped():
                self.progress.grid_remove()

            # Details of the file being transcribed.
            meta = ""
            if s["file"] and self.engine.inbox:
                meta = media_summary(self.details.get(self.engine.inbox / s["file"]), full=True)
            if meta:
                self.meta_text.configure(text=meta)
                if not self.meta_text.winfo_ismapped():
                    self.meta_text.grid()
            elif self.meta_text.winfo_ismapped():
                self.meta_text.grid_remove()

            if s["file"]:
                self._progress_mode("determinate")
                self.file_text.configure(text=shorten(s["file"], 70))
                self.progress.set(s["percent"] / 100)
                detail = f"{s['percent']:.0f}%"
                if s["eta"]:
                    left = format_duration(s["eta"])
                    detail += f"  ·  {'about ' if left[0].isdigit() else ''}{left} left"
                if phase == "paused":
                    detail += "  ·  paused"
                self.detail_text.configure(text=detail, text_color=GRAY)
            elif phase == "error":
                self._progress_mode("determinate")
                self.progress.set(0)
                self.file_text.configure(text="Something needs fixing")
                self.detail_text.configure(text=s["error"] or "", text_color=RED)
            else:
                busy = phase in ("loading", "starting", "stopping")
                self._progress_mode("indeterminate" if busy else "determinate")
                if not busy:
                    self.progress.set(0)
                if phase == "idle" and not s["queue"]:
                    self.file_text.configure(text="All caught up")
                    self.detail_text.configure(
                        text="Drop audio files into the Inbox or onto this window, or click "
                             "Add files.", text_color=GRAY)
                elif phase == "paused":
                    self.file_text.configure(text="Paused")
                    self.detail_text.configure(text="Click Resume to continue.", text_color=GRAY)
                else:
                    self.file_text.configure(text=s["message"].split(" – ")[0])
                    self.detail_text.configure(
                        text=(s["message"].split(" – ", 1)[1].capitalize()
                              if " – " in s["message"] else ""),
                        text_color=GRAY)

            left = [f"This PC: {T.HOSTNAME}"]
            if s["model"]:
                left.insert(0, f"Model: {s['model']}")
            if self.copying:
                left.insert(0, f"Copying {self.copying} file{'s' if self.copying > 1 else ''} "
                               "into the Inbox...")
            self.bar_left.configure(text="   ·   ".join(left))
            if self.tray:
                self.tray.title = self.tray_tooltip(s, title)

        def tray_tooltip(self, s, title):
            """e.g. "Transcribing 3 of 12 · 45%". Counts files in the current
            batch: it starts again once the queue has run empty."""
            current = s["file"]
            if current != self._batch_file:
                if self._batch_file:
                    self._batch_done += 1
                self._batch_file = current
            if not current and not s["queue"]:
                self._batch_done = 0
            if current:
                pos = self._batch_done + 1
                total = pos + len(s["queue"])
                text = f"{title} {pos} of {total} · {s['percent']:.0f}%"
                if s["phase"] == "paused":
                    text = f"Paused · {pos} of {total} · {s['percent']:.0f}%"
            elif s["queue"]:
                text = f"{title} · {len(s['queue'])} waiting"
            elif s["phase"] == "idle":
                text = "All caught up"
            else:
                text = title
            return f"{T.APP_NAME}\n{text}"[:127]  # Windows limit

        def _show(self, widget, visible, **pack):
            if visible and not widget.winfo_ismapped():
                widget.pack(**pack)
            elif not visible and widget.winfo_ismapped():
                widget.pack_forget()

        def _progress_mode(self, mode):
            if mode == self.progress_mode:
                return
            self.progress_mode = mode
            if mode == "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start()
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate")

        def refresh_tab_labels(self, s):
            counts = {"Queue": len(s["queue"]), "Issues": len(s["issues"])}
            labels = [f"{n} ({counts[n]})" if counts.get(n) else n for n in self.tab_names]
            if labels != list(self.tabs.cget("values")):
                self.tabs.configure(values=labels)
                self.tabs.set(labels[self.tab_names.index(self.current_tab)])

        def refresh_tabs(self, s=None, force=False):
            s = s or self.engine.snapshot()
            if time.time() - self.last_completed_scan > 5 or force:
                self.scan_completed()
            if time.time() - self.details_checked > 1:
                self.details_checked = time.time()
                self.details_stamp = self.details.version
            tab = self.current_tab
            if tab == "Queue":
                key = (tuple(s["queue"]), int(time.time() // 30), self.details_stamp)
                build = self._build_queue_rows
                data = s["queue"]
            elif tab == "Completed":
                # Apply what was typed once typing pauses (not on every key).
                typed = self.search_entry.get().strip().lower()
                if typed != self.search_query and time.time() - self.search_typed > 0.3:
                    self.search_query = typed
                data = self.filtered_completed()
                key = (tuple((p.name, int(m)) for m, p, _ in data), self.search_query,
                       self.date_filter.get(), int(time.time() // 30), self.details_stamp)
                build = self._build_completed_rows
            elif tab == "Issues":
                key = tuple(sorted(s["issues"].items()))
                build = self._build_issue_rows
                data = s["issues"]
            else:
                return
            if self.view_keys.get(tab) != key:
                self.view_keys[tab] = key
                frame = self.list_frames[tab]
                for child in frame.winfo_children():
                    child.destroy()
                build(frame, data)

        def _row(self, frame):
            row = ctk.CTkFrame(frame, corner_radius=8, fg_color=("gray92", "gray17"))
            row.pack(fill="x", pady=3, padx=2)
            return row

        def _empty(self, frame, title, text):
            ctk.CTkLabel(frame, text=title, font=font(14, "bold")).pack(pady=(36, 4))
            ctk.CTkLabel(frame, text=text, text_color=GRAY, justify="center").pack()

        def _build_queue_rows(self, frame, items):
            if not items:
                self._empty(frame, "The queue is empty",
                            "Files dropped into the Inbox appear here while they wait.")
                return
            for i, (name, size, arrived) in enumerate(items):
                row = self._row(frame)
                ctk.CTkLabel(row, text=f"{i + 1}", width=28, text_color=GRAY
                             ).pack(side="left", padx=(8, 0))
                text = ctk.CTkFrame(row, fg_color="transparent")
                text.pack(side="left", fill="x", expand=True, pady=6)
                ctk.CTkLabel(text, text=shorten(name, 58), anchor="w").pack(fill="x")
                info = self.details.get(self.engine.inbox / name) if self.engine.inbox else None
                ctk.CTkLabel(text, text=f"{media_summary(info)}  \u00b7  added "
                                        f"{format_when(arrived)}",
                             anchor="w", text_color=GRAY, font=font(11)).pack(fill="x")
                ghost_button(row, "Skip", lambda n=name: self.skip_queued(n), width=56
                             ).pack(side="right", padx=(4, 10))
                if i > 0:
                    ghost_button(row, "Do next", lambda n=name: self.engine.do_next(n), width=72
                                 ).pack(side="right", padx=4)

        def skip_queued(self, name):
            if messagebox.askyesno("Skip this file?",
                                   f"Take {name} out of the queue?\n\nThe file stays in the "
                                   "Inbox; you can retry it from the Issues tab.", parent=self):
                self.engine.skip_waiting(name)

        def clear_search(self, event=None):
            self.search_entry.delete(0, "end")
            self.search_typed = 0.0
            self.focus_set()  # lets the grey hint text show again

        def guess_source(self, path):
            """For transcripts made before the index existed: match by name."""
            if not self.engine.inbox:
                return None
            for ext in self.config_raw().get("file_types", []):
                if (self.engine.inbox / (path.stem + ext)).exists():
                    self.source_of[path.name.lower()] = path.stem + ext
                    return path.stem + ext
            return None

        def filtered_completed(self):
            """Transcripts matching the search and date filter: (mtime, path, snippet).
            Remembered until something changes, as this runs 5 times a second."""
            memo_key = (self.search_query, self.date_filter.get(), id(self.completed),
                        self.details_stamp, int(time.time() // 30))
            if getattr(self, "_filter_memo", (None,))[0] == memo_key:
                return self._filter_memo[1]
            result = self._filter_completed()
            self._filter_memo = (memo_key, result)
            return result

        def _filter_completed(self):
            items = self.completed
            days = DATE_FILTERS.get(self.date_filter.get())
            if days is not None:
                if days == 0:
                    cutoff = datetime.combine(datetime.now().date(), datetime.min.time()
                                              ).timestamp()
                else:
                    cutoff = time.time() - days * 86400
                items = [(m, p) for m, p in items if m >= cutoff]
            query = self.search_query
            total = len(items)
            if not query:
                self.result_text.configure(
                    text=f"{total:,} transcript{'s' if total != 1 else ''}"
                         + ("  \u00b7  showing the newest 100" if total > 100 else ""))
                return [(m, p, None) for m, p in items[:100]]

            results, pending = [], 0
            for mtime, path in items:
                snippet = None
                if query in path.name.lower():
                    results.append((mtime, path, None))
                else:
                    text = self.details.get(path, read_text_lower)
                    if text is None:
                        pending += 1  # still being read in the background
                        continue
                    lower = text.get("lower", "")
                    pos = lower.find(query)
                    if pos >= 0:
                        raw = text["text"] if len(text["text"]) == len(lower) else lower
                        start = max(0, pos - 50)
                        piece = " ".join(raw[start:pos + len(query) + 70].split())
                        snippet = ("\u2026" if start else "") + piece + "\u2026"
                        results.append((mtime, path, f"\u201c{snippet}\u201d"))
                if len(results) >= 200:
                    break
            label = f"{len(results):,} match{'es' if len(results) != 1 else ''} for \u201c{query}\u201d"
            if pending:
                label += f"  \u00b7  searching {pending:,} more..."
            self.result_text.configure(text=label)
            return results

        def _build_completed_rows(self, frame, items):
            if not items and (self.search_query or self.date_filter.get() != "Any time"):
                self._empty(frame, "No matching transcripts",
                            "Try other words, or a longer time period.")
                return
            if not items:
                self._empty(frame, "No transcripts yet",
                            "Finished transcripts from this PC (and any other PC sharing the "
                            "folder) appear here.")
                return
            for mtime, path, snippet in items:
                row = self._row(frame)
                text = ctk.CTkFrame(row, fg_color="transparent")
                text.pack(side="left", fill="x", expand=True, padx=(14, 0), pady=6)
                ctk.CTkLabel(text, text=shorten(path.name, 58), anchor="w").pack(fill="x")
                if snippet:
                    ctk.CTkLabel(text, text=snippet, anchor="w", justify="left",
                                 font=font(11, "normal"), text_color=("gray20", "gray80"),
                                 wraplength=430).pack(fill="x")
                parts = []
                duration = None
                source = self.source_of.get(path.name.lower()) or self.guess_source(path)
                if source and self.engine.inbox:
                    audio = self.engine.inbox / source
                    if audio.exists():
                        info = self.details.get(audio)
                        duration = (info or {}).get("duration")
                        parts.append(media_summary(info))
                words = (self.details.get(path, count_words) or {}).get("words")
                if words is not None:
                    parts.append(f"{words:,} words")
                parts.append(format_when(mtime))
                took = self.took.get(path.name)
                if took and duration and duration >= 60:
                    parts.append(f"{format_duration(duration)} audio \u2192 "
                                 f"{format_duration(took)}")
                elif took:
                    parts.append(f"took {format_duration(took)}")
                meta = "  \u00b7  ".join(parts)
                ctk.CTkLabel(text, text=meta, anchor="w", text_color=GRAY, font=font(11)
                             ).pack(fill="x")
                ghost_button(row, "Show in folder", lambda p=path: show_in_folder(p), width=104
                             ).pack(side="right", padx=(4, 10))
                ctk.CTkButton(row, text="Open", width=60, height=26,
                              command=lambda p=path: self.review_transcript(p)
                              ).pack(side="right", padx=4)

        def _build_issue_rows(self, frame, issues):
            if not issues:
                self._empty(frame, "No issues",
                            "Files that fail or that you skip are listed here so you can "
                            "retry them.")
                return
            top = ctk.CTkFrame(frame, fg_color="transparent")
            top.pack(fill="x", pady=(0, 4))
            ghost_button(top, "Retry all", self.retry_all, width=80).pack(side="right", padx=8)
            ghost_button(top, "Send report", self.send_report, width=100
                         ).pack(side="right", padx=(8, 0))
            items = sorted(issues.items(), key=lambda kv: kv[1][2], reverse=True)
            for name, (kind, reason, when) in items:
                row = self._row(frame)
                text = ctk.CTkFrame(row, fg_color="transparent")
                text.pack(side="left", fill="x", expand=True, padx=(14, 0), pady=6)
                ctk.CTkLabel(text, text=shorten(name, 58), anchor="w").pack(fill="x")
                label, colour = ("Failed", RED) if kind == "failed" else ("Skipped", AMBER)
                status = ctk.CTkFrame(text, fg_color="transparent")
                status.pack(fill="x")
                ctk.CTkLabel(status, text=label, text_color=colour, font=font(11, "bold")
                             ).pack(side="left")
                ctk.CTkLabel(status, text=f"  \u00b7  {format_when(when)}", text_color=GRAY,
                             font=font(11)).pack(side="left")
                ctk.CTkLabel(text, text=friendly_error(reason) if kind == "failed" else reason,
                             anchor="w", justify="left",
                             text_color=GRAY, font=font(11), wraplength=440).pack(fill="x")
                ctk.CTkButton(row, text="Retry", width=64, height=26,
                              command=lambda n=name: self.retry_one(n)
                              ).pack(side="right", padx=(4, 10))

        def send_report(self):
            """Zip the log, settings and PC details onto the Desktop to email."""
            if not messagebox.askokcancel(
                    "Create a problem report",
                    "This saves a small zip file on your Desktop with the program's log, "
                    "its settings and basic details about this PC, for you to email to "
                    "whoever looks after Audio Transcriber.\n\nIt contains file names "
                    "and folder paths, but no audio and no transcripts.", parent=self):
                return
            desktop = Path.home() / "Desktop"
            try:
                import ctypes.wintypes
                buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
                if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                    desktop = Path(buf.value)  # Desktop may be in OneDrive
            except Exception:
                pass
            try:
                path = make_report_zip(self.engine.snapshot()["issues"], desktop)
            except OSError as e:
                messagebox.showerror("Create a problem report",
                                     f"Couldn't create the report:\n{e}", parent=self)
                return
            log.info("Problem report saved: %s", path)
            show_in_folder(path)
            messagebox.showinfo("Report saved",
                                f"Saved to your Desktop:\n{path.name}\n\nAttach it to an email "
                                "describing what went wrong.", parent=self)

        def retry_one(self, name):
            self.engine.retry([name])
            log.info("Retrying %s", name)

        def scan_completed(self):
            self.last_completed_scan = time.time()
            try:
                files = [p for p in self.output_dir().glob("*.txt") if not p.name.startswith(".")]
                self.completed = sorted(((p.stat().st_mtime, p) for p in files),
                                        key=lambda mp: mp[0], reverse=True)
            except OSError:
                self.completed = []
            try:
                import json
                index = self.output_dir() / T.OutputNames.INDEX_NAME
                with open(index, encoding="utf-8") as f:
                    self.source_of = {k.lower(): v for k, v in json.load(f).items()}
            except (OSError, ValueError):
                self.source_of = {}
            self.took.update(load_timings(self.output_dir()))

        def done_today(self):
            today = datetime.now().date()
            count = 0
            for mtime, _ in self.completed:  # newest first
                if datetime.fromtimestamp(mtime).date() != today:
                    break
                count += 1
            return count

        def drain_log(self):
            lines = []
            while not self.log_lines.empty() and len(lines) < 200:
                lines.append(self.log_lines.get_nowait())
            if not lines:
                return
            self.activity.configure(state="normal")
            for level, line in lines:
                tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
                self.activity.insert("end", line + "\n", tag)
            # Keep the box from growing forever.
            if int(self.activity.index("end-1c").split(".")[0]) > 1000:
                self.activity.delete("1.0", "200.0")
            self.activity.see("end")
            self.activity.configure(state="disabled")

        def on_engine_event(self, kind, data):
            name = data["audio"].name
            if kind == "done":
                self.took[data["txt"].name] = data["seconds"]
                save_timing(data["txt"].parent, data["txt"].name, data["seconds"])
                self.notify("Transcript ready", f"{name} ({format_duration(data['seconds'])})")
            elif kind == "failed":
                self.notify("Could not transcribe", f"{name}: {shorten(data['error'], 120)}")
            self.last_completed_scan = 0.0  # rescan now

        # =================================================================
        # Tray, closing, quitting
        # =================================================================

        def start_tray(self):
            try:
                import pystray
            except Exception as e:
                log.warning("Tray icon unavailable: %s", e)
                return

            def call(fn):
                return lambda icon, item: self.ui_calls.put(fn)

            menu = pystray.Menu(
                pystray.MenuItem("Open Audio Transcriber", call(self.show_window), default=True),
                pystray.MenuItem("Pause", call(self.toggle_pause),
                                 checked=lambda item: self.engine.paused),
                pystray.MenuItem("Add files...", call(lambda: (self.show_window(), self.add_files()))),
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
            if self.state() == "iconic":
                self.state("normal")
            self.lift()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))
            self.focus_force()

        def on_close(self):
            """The X button: keep running in the tray (or quit if there's no tray)."""
            if not self.tray:
                self.quit_app()
                return
            self.withdraw()
            if not self.told_about_tray:
                self.told_about_tray = True
                try:
                    self.tray.notify("Still running in the background. Right-click the tray "
                                     "icon and choose Quit to stop it.", T.APP_NAME)
                except Exception:
                    pass

        def quit_app(self, force=False):
            """Quit. force=True (from the installer) skips the questions."""
            if self.quitting:
                return
            if force:
                self.pending.clear()
                if self.engine.snapshot()["file"]:
                    log.info("Closing for an update; the current file will be "
                             "started again next time.")
            if self.pending:
                self.show_window()
                answer = messagebox.askyesnocancel(
                    "Unsaved settings",
                    "You changed some settings but didn't click Apply.\n\n"
                    "Apply them before quitting?", parent=self)
                if answer is None:
                    return
                if answer:
                    self.apply_changes()
                    if self.pending:  # cancelled or failed
                        return
            busy = self.engine.snapshot()["file"]
            if busy and not force:
                self.show_window()
                if not messagebox.askyesno(
                        "Quit Audio Transcriber?",
                        f"{busy} is still being transcribed.\n\nIf you quit now it starts "
                        "again from the beginning next time.", icon="warning", parent=self):
                    return
            self.quitting = True
            log.info("Quitting...")
            self.engine.stop()
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.withdraw()
            started = time.time()

            def wait_then_close():
                if self.engine.thread.is_alive() and time.time() - started < 15:
                    self.after(200, wait_then_close)
                else:
                    self.destroy()

            wait_then_close()

    return App


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def show_error(message):
    try:
        ctypes.windll.user32.MessageBoxW(None, message, T.APP_NAME, 0x10)
    except Exception:
        print(message)


def run_console():
    """--console: the original text mode. The .exe has no console of its own,
    so attach to the command prompt it was started from, or open a new one."""
    if getattr(sys, "frozen", False):
        kernel32 = ctypes.windll.kernel32
        ATTACH_PARENT_PROCESS = -1
        if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r")
    T.cli()


QUALITY_PRESETS = [
    # label, model, description
    ("Fast", "small",
     "Quickest. Good for clear speech. About 480 MB download."),
    ("Balanced", "medium",
     "Recommended. Good for Tagalog, Taglish and English. About 1.5 GB."),
    ("Most accurate", "large-v3",
     "Best results, but slow without an NVIDIA graphics card. About 3 GB."),
]


def run_setup_wizard():
    """First launch: pick the Inbox, the Transcripts folder and a quality.
    Returns the chosen settings, or None if the window was closed."""
    import customtkinter as ctk
    from tkinter import filedialog

    defaults = T.DEFAULT_CONFIG
    choice = {"input_folder": defaults["input_folder"],
              "output_folder": defaults["output_folder"],
              "model_size": "medium"}
    result = {}

    win = ctk.CTk()
    win.title(f"Set up {T.APP_NAME}")
    win.geometry("580x480")
    win.resizable(False, False)
    try:
        from PIL import ImageTk
        win._icon = ImageTk.PhotoImage(make_icon_image(64))
        win.iconphoto(True, win._icon)
    except Exception:
        pass

    step_lbl = ctk.CTkLabel(win, text="", text_color=GRAY)
    step_lbl.pack(anchor="w", padx=28, pady=(22, 0))
    body = ctk.CTkFrame(win, fg_color="transparent")
    nav = ctk.CTkFrame(win, fg_color="transparent")
    nav.pack(side="bottom", fill="x", padx=28, pady=20)
    back_btn = ctk.CTkButton(nav, text="Back", width=90, fg_color="transparent",
                             border_width=1, text_color=("gray10", "gray90"))
    next_btn = ctk.CTkButton(nav, text="Next", width=110)
    next_btn.pack(side="right")
    body.pack(fill="both", expand=True, padx=28, pady=(4, 0))
    page = [0]

    def heading(text, sub):
        ctk.CTkLabel(body, text=text, font=ctk.CTkFont(size=20, weight="bold"),
                     anchor="w").pack(fill="x", pady=(4, 6))
        ctk.CTkLabel(body, text=sub, anchor="w", justify="left", wraplength=500,
                     text_color=GRAY).pack(fill="x", pady=(0, 16))

    def folder_page(key, title, sub):
        heading(title, sub)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        entry = ctk.CTkEntry(row, height=34)
        entry.insert(0, choice[key])
        entry.configure(state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def browse():
            path = filedialog.askdirectory(parent=win, initialdir=choice[key],
                                           title=title, mustexist=False)
            if path:
                choice[key] = str(Path(path))
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, choice[key])
                entry.configure(state="readonly")
        ctk.CTkButton(row, text="Change...", width=100, height=34,
                      command=browse).pack(side="right")
        ctk.CTkLabel(body, text="The folder is created if it doesn't exist. "
                                "A shared network folder works too.",
                     anchor="w", text_color=GRAY).pack(fill="x", pady=(10, 0))

    def quality_page():
        heading("How accurate should transcripts be?",
                "You can change this later in Settings.")
        var = ctk.StringVar(value=choice["model_size"])
        for label, model, desc in QUALITY_PRESETS:
            card = ctk.CTkFrame(body, corner_radius=8)
            card.pack(fill="x", pady=4)
            ctk.CTkRadioButton(card, text=label, variable=var, value=model,
                               font=ctk.CTkFont(weight="bold"),
                               command=lambda m=model: choice.update(model_size=m)
                               ).pack(anchor="w", padx=12, pady=(10, 0))
            ctk.CTkLabel(card, text=desc, anchor="w", justify="left", wraplength=440,
                         text_color=GRAY).pack(fill="x", padx=42, pady=(0, 8))

    pages = [
        lambda: folder_page("input_folder", "Where will you put audio files?",
                            "This is your Inbox. Any audio or video file placed here "
                            "is transcribed automatically."),
        lambda: folder_page("output_folder", "Where should transcripts go?",
                            "Finished transcripts are saved here as text files."),
        quality_page,
    ]

    def show():
        for w in body.winfo_children():
            w.destroy()
        step_lbl.configure(text=f"Step {page[0] + 1} of {len(pages)}")
        pages[page[0]]()
        last = page[0] == len(pages) - 1
        next_btn.configure(text="Start" if last else "Next")
        if page[0]:
            back_btn.pack(side="left")
        else:
            back_btn.pack_forget()

    def go(delta):
        if page[0] == 1 and delta > 0 and (
                Path(choice["output_folder"]) == Path(choice["input_folder"])):
            from tkinter import messagebox
            messagebox.showwarning(T.APP_NAME, "Please pick a different folder from the "
                                   "Inbox, so transcripts don't get mixed in with the "
                                   "audio files.", parent=win)
            return
        if page[0] == len(pages) - 1 and delta > 0:
            result.update(choice)
            win.destroy()
            return
        page[0] += delta
        show()

    next_btn.configure(command=lambda: go(1))
    back_btn.configure(command=lambda: go(-1))
    win.bind("<Return>", lambda e: go(1))
    show()
    win.mainloop()
    return result or None


def run_window(start_hidden):
    # Sharper text on high-resolution screens.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    T.setup_logging()
    if not T.CONFIG_PATH.exists() and not start_hidden:
        import customtkinter as ctk
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        chosen = run_setup_wizard()
        if not chosen:
            sys.exit(0)  # closed before finishing: ask again next time
        for key in ("input_folder", "output_folder"):
            try:
                Path(chosen[key]).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                show_error(f"Couldn't create the folder:\n{chosen[key]}\n\n{e}")
                sys.exit(1)
        T.save_raw_config({**T.DEFAULT_CONFIG, **chosen})
        log.info("First-run setup saved: %s", chosen)
    try:
        config = T.load_config()
        inbox = Path(config["input_folder"])
        T.ensure_single_instance(inbox)
    except T.UserError as e:
        # Already running? Bring that window to the front instead.
        if "already watching" in str(e) and ask_running_copy_to_show(inbox):
            sys.exit(0)
        show_error(str(e))
        sys.exit(1)

    log.info("=" * 60)
    log.info("%s v%s starting (window)", T.APP_NAME, T.__version__)

    import customtkinter as ctk
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")

    engine = Engine(locked_folder=inbox)
    app = build_app_class()(engine, start_hidden=start_hidden)
    listen_for_show_requests(inbox, lambda: app.ui_calls.put(app.show_window))
    listen_for_event(QUIT_EVENT, lambda: app.ui_calls.put(lambda: app.quit_app(force=True)))
    engine.start()
    app.mainloop()


def main():
    args = sys.argv[1:]
    if "--quit" in args:  # used by the installer and uninstaller
        sys.exit(0 if ask_running_copies_to_quit() else 1)
    # Lets --quit (and the installer) see that the program is running.
    try:
        main.running_flag = ctypes.windll.kernel32.CreateMutexW(None, False, RUNNING_MUTEX)
    except Exception:
        pass
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
