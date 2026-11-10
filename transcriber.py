"""
Audio Transcriber
=================

Watches a folder for audio files (.mp3, .mp4, .m4a, .wav, ...) and
transcribes each one to a .txt file using faster-whisper.

How it works:
  1. Reads settings from config.json (created with defaults if missing).
  2. Loads the Whisper model once (GPU if available, otherwise CPU).
  3. Transcribes every audio file in the input folder that has no .txt yet.
  4. Keeps watching the input folder and transcribes new files as they arrive.

The original audio files are only ever READ. They are never changed, moved,
or deleted.
"""

import hashlib
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import time
import traceback
from pathlib import Path

# Bump this for each release (and add an entry to CHANGELOG.md). The release
# build checks that the git tag (e.g. v1.0.0) matches it.
__version__ = "1.10.3"
APP_NAME = "Audio Transcriber"

# This PC's name. Written into claim files so PCs sharing a folder can tell
# whose work is whose (only letters, digits, "-" and "_" are kept).
HOSTNAME = re.sub(r"[^A-Za-z0-9_-]", "_", socket.gethostname()) or "pc"


class Stopped(Exception):
    """Raised inside a transcription when the user quits mid-file."""


class Skipped(Exception):
    """Raised inside a transcription when the user skips the current file."""


class Hooks:
    """How the engine reports to (and is paused by) the window.

    The console version uses these no-op defaults; the window version
    overrides them.
    """

    def checkpoint(self) -> None:
        """Called often while working. May block (pause) or raise Stopped."""

    def progress(self, audio: Path, percent: float) -> None:
        """Progress of the current file, 0-100."""

    def event(self, kind: str, **data) -> None:
        """Something happened: "loading", "ready", "started", "done", "failed",
        "idle", "queue"."""

    def sleep(self, seconds: float) -> None:
        """Wait between folder checks. May return early (or raise Stopped)."""
        time.sleep(seconds)

    def pick(self, ready: list) -> Path:
        """Which ready file to do next (the window can move one to the front)."""
        return ready[0]

    def should_restart(self) -> bool:
        """True when settings changed and the main loop should return, so it
        can be restarted with them. Only checked between files."""
        return False


NO_HOOKS = Hooks()


class UserError(Exception):
    """A problem the user can fix (bad config, already running, ...).
    Shown as a plain message instead of a full error report."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_app_dir() -> Path:
    """Folder that holds config.json, transcriber.log and models/.

    When running as a PyInstaller .exe this is the folder the .exe is in.
    When running as a normal script it is the folder of this .py file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "transcriber.log"
MODELS_DIR = APP_DIR / "models"

# Let Windows find optional CUDA DLLs (cuBLAS / cuDNN) placed next to the .exe
# or in a "cuda" subfolder. This must happen before ctranslate2 is imported.
for _dll_dir in (APP_DIR, APP_DIR / "cuda"):
    if _dll_dir.is_dir():
        os.environ["PATH"] = str(_dll_dir) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(_dll_dir))
        except (AttributeError, OSError):
            pass

# Quieter Hugging Face downloads on Windows.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_documents_dir() -> Path:
    """The current user's Documents folder.

    Asks Windows for it, because Documents may have been moved (e.g. into
    OneDrive). Falls back to <home>\\Documents.
    """
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        CSIDL_PERSONAL = 5  # "My Documents"
        if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Documents"


DOCUMENTS_DIR = get_documents_dir()

DEFAULT_CONFIG = {
    # Defaults: Documents\Audio Transcriber\Inbox and \Transcripts of
    # whoever runs it.
    "input_folder": str(DOCUMENTS_DIR / "Audio Transcriber" / "Inbox"),
    "output_folder": str(DOCUMENTS_DIR / "Audio Transcriber" / "Transcripts"),
    # tiny, base, small, medium, large-v3
    "model_size": "small",
    # "auto" = detect the language. Or force one, e.g. "en" or "tl" (Tagalog).
    "language": "auto",
    "include_timestamps": False,
    # Put [MUSIC] in front of lines where music or singing dominates (songs,
    # jingles). Everything is still transcribed.
    "label_music": False,
    # Skip silence. Good for speech recordings, but it also throws away
    # singing over music - set to false for songs.
    "vad_filter": True,
    # Give the model the cleaned-up file name (e.g. "Olivia Rodrigo - drop
    # dead") as a hint. Helps it spell names and song titles correctly.
    "use_filename_hint": True,
    # Which files to pick up. Audio is pulled out of video files automatically.
    "file_types": [".mp3", ".mp4", ".m4a", ".wav", ".flac", ".ogg", ".opus",
                   ".webm", ".aac", ".wma", ".mkv", ".mov"],
    # Which waiting file to do next: "newest_first" (fresh recordings jump
    # the queue), "oldest_first" or "name" (alphabetical).
    "order": "newest_first",
    # "fast" = take the model's single best guess at each step (~35% faster,
    # same accuracy on clear speech in testing). "accurate" = compare 5
    # guesses (beam search); can help with noisy audio, music or accents.
    "speed": "fast",
    "poll_interval_seconds": 10,
    # "auto" = GPU if available, else CPU. Or force "cuda" or "cpu".
    "device": "auto",
    # Once a day, check GitHub for a newer version and show a notice.
    "check_for_updates": True,
}

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
# speed setting -> beam size (how many candidate wordings Whisper compares)
SPEED_BEAM_SIZE = {"fast": 1, "accurate": 5}
VALID_ORDERS = {"newest_first", "oldest_first", "name"}


def read_raw_config() -> dict:
    """config.json exactly as written (created with defaults if missing)."""
    if not CONFIG_PATH.exists():
        save_raw_config(DEFAULT_CONFIG)
        log.info("Created default config file: %s", CONFIG_PATH)

    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise UserError(
                f"{CONFIG_PATH} is not valid JSON ({e}).\n"
                "Tip: backslashes in Windows paths must be doubled, "
                r'e.g. "C:\\Users\\YourName\\Documents\\Audio Transcriber\\Inbox"'
            ) from None


def save_raw_config(raw: dict) -> None:
    """Write config.json safely (temp file + rename, so it's never half-written)."""
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=4)
    os.replace(tmp, CONFIG_PATH)


def update_config(changes: dict) -> None:
    """Change some settings in config.json, keeping everything else."""
    raw = read_raw_config()
    raw.update(changes)
    save_raw_config(raw)


def load_config() -> dict:
    """Load config.json, creating it with defaults if it doesn't exist.

    Missing keys are filled in from DEFAULT_CONFIG, so an old config file
    keeps working after new settings are added.
    """
    user_config = read_raw_config()

    config = dict(DEFAULT_CONFIG)
    config.update(user_config)

    # Basic checks so mistakes give a clear message.
    if config["model_size"] not in VALID_MODELS:
        logging.warning(
            "model_size '%s' is not one of %s - trying it anyway.",
            config["model_size"], sorted(VALID_MODELS),
        )
    try:
        config["poll_interval_seconds"] = max(1, int(config["poll_interval_seconds"]))
    except (TypeError, ValueError):
        config["poll_interval_seconds"] = DEFAULT_CONFIG["poll_interval_seconds"]

    lang = config.get("language")
    if not lang or str(lang).strip().lower() in ("auto", "none", ""):
        config["language"] = None  # None tells faster-whisper to auto-detect
    else:
        config["language"] = str(lang).strip().lower()

    config["speed"] = str(config.get("speed") or "").strip().lower()
    if config["speed"] not in SPEED_BEAM_SIZE:
        raise UserError(f'"speed" in {CONFIG_PATH} must be "fast" or "accurate"')

    config["order"] = str(config.get("order") or "").strip().lower()
    if config["order"] not in VALID_ORDERS:
        raise UserError(
            f'"order" in {CONFIG_PATH} must be one of: {", ".join(sorted(VALID_ORDERS))}'
        )

    config["device"] = str(config.get("device") or "auto").strip().lower()
    config["include_timestamps"] = bool(config["include_timestamps"])
    config["vad_filter"] = bool(config["vad_filter"])
    config["label_music"] = bool(config.get("label_music"))
    config["use_filename_hint"] = bool(config["use_filename_hint"])
    # Normalise to lowercase ".ext" so "MP4" or "mp4" both work.
    config["file_types"] = {
        "." + str(ext).strip().lower().lstrip(".") for ext in config["file_types"]
    }
    return config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Log to the console (short messages) and transcriber.log (with times)."""
    # Make sure the console can print characters like "→" without crashing.
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Show our own messages, but only warnings/errors from other libraries
    # (otherwise the model download prints dozens of "HTTP Request" lines).
    def only_ours(record):
        return record.name.startswith("transcriber") or record.levelno >= logging.WARNING

    if sys.stdout is not None:  # no console in the window version
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        console.addFilter(only_ours)
        root.addHandler(console)
    setup_logging.only_ours = only_ours  # reused by the window's activity box

    # The log file is capped: when it reaches 1 MB it is renamed to
    # transcriber.log.1 (older ones to .2, .3) and a new one is started.
    logfile = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logfile.addFilter(only_ours)
    root.addHandler(logfile)

    # Full error details (tracebacks) go to the log file only, not the console.
    file_only.addHandler(logfile)
    file_only.propagate = False


log = logging.getLogger("transcriber")
file_only = logging.getLogger("transcriber.details")


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def cuda_available() -> bool:
    """True if CTranslate2 can see an NVIDIA GPU."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def try_load_model(model_size: str, device: str, compute_type: str):
    """Load the model and run a tiny test so missing GPU libraries fail now,
    not halfway through the first real file."""
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        download_root=str(MODELS_DIR),
    )
    # 1 second of silence. language="en" skips detection; list() forces the
    # work to actually run (segments are generated lazily).
    silence = np.zeros(16000, dtype=np.float32)
    segments, _ = model.transcribe(silence, language="en", beam_size=1)
    list(segments)
    return model


def load_model(config: dict):
    """Pick GPU or CPU and load the Whisper model once."""
    model_size = config["model_size"]
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    want = config["device"]
    use_gpu = want == "cuda" or (want == "auto" and cuda_available())

    log.info("Loading model '%s' (first run downloads it into %s)...", model_size, MODELS_DIR)
    start = time.time()

    if use_gpu:
        try:
            model = try_load_model(model_size, "cuda", "float16")
            log.info("Model loaded on GPU (CUDA, float16) in %.0fs", time.time() - start)
            return model
        except Exception as e:
            log.warning("GPU could not be used (%s). Falling back to CPU.", e)
            log.warning("See README.md -> 'Using an NVIDIA GPU' to fix this.")

    model = try_load_model(model_size, "cpu", "int8")
    log.info("Model loaded on CPU (int8) in %.0fs", time.time() - start)
    return model


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    """12.5 -> '00:00:12'"""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# Words that often appear in downloaded file names but aren't part of the
# song/recording title, e.g. "katy-perry-thinking-of-you-ytmp4.savetube.vip".
FILENAME_JUNK = {
    "official", "video", "audio", "lyrics", "lyric", "music", "mv", "hd", "hq",
    "visualizer", "savetube", "vip", "y2mate", "com", "net", "download", "feat", "ft",
    # Recorder / logger names like "2026-04-26 00-00-00 DWCM - FM (Backup)"
    "backup", "fm", "am", "rec", "recording", "copy", "final", "track",
}


def filename_hint(path: Path):
    """'katy-perry-thinking-of-you-ytmp4.savetube.vip.mp4' -> 'katy perry thinking of you'

    Returns None if nothing useful is left (e.g. 'meeting1.mp3').
    """
    words = []
    for word in re.split(r"[\s\-_.()\[\]{}]+", path.stem):
        lower = word.lower()
        if not word or lower in FILENAME_JUNK:
            continue
        # Skip numbers (dates, times) and codes like "ytmp4" or "1080p".
        if any(c.isdigit() for c in word):
            continue
        words.append(word)
    # One leftover word (e.g. a station's call sign) says too little, and a
    # meaningless hint makes Whisper skip whole stretches of audio.
    if len(words) < 2:
        return None
    hint = " ".join(words)[:100]
    # An all-lowercase hint makes the model write the whole transcript in
    # lowercase, so capitalise it ("katy perry" -> "Katy Perry").
    if hint == hint.lower():
        hint = hint.title()
    return hint if len(hint) >= 3 else None


# Lines Whisper is known to invent during music or silence (it learned them
# from YouTube subtitles). A segment that is exactly one of these is dropped.
HALLUCINATIONS = {
    "thanks for watching", "thank you for watching", "to be continued",
    "please subscribe", "subscribe to my channel", "like and subscribe",
    "please like and subscribe",
}


def is_hallucination(text: str) -> bool:
    norm = " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())
    return norm in HALLUCINATIONS or norm.startswith("subtitles by") or "amara org" in norm


_music_detector = None


def find_music(audio, hooks: Hooks):
    """Music scores for the audio, or None if music detection isn't possible
    (the transcript is then simply written without labels)."""
    global _music_detector
    try:
        import music_detect
        if _music_detector is None:
            _music_detector = music_detect.MusicDetector(music_detect.ensure_model(MODELS_DIR))
        started = time.time()
        scores = _music_detector.scan(audio, hooks.checkpoint)
        log.info("   Music detection: %d%% of the file is music (%.0fs)",
                 100 * sum(1 for m, s in scores if m >= 0.5) / max(1, len(scores)),
                 time.time() - started)
        return scores
    except (Stopped, Skipped):
        raise
    except Exception as e:
        log.warning("   Music detection didn't work (%s); transcribing without "
                    "music labels.", e)
        return None


def transcribe_file(model, audio_path: Path, txt_path: Path, config: dict,
                    hooks: Hooks = NO_HOOKS) -> None:
    """Transcribe one file (see _transcribe_file), checking for Pause, Skip
    and Stop before every 30-second chunk Whisper reads. Without this they
    only take effect when a line of text comes out, which during a long
    stretch of silence or music without words can take many minutes."""
    original = model.encode

    def encode_checked(*args, **kwargs):
        hooks.checkpoint()
        return original(*args, **kwargs)

    model.encode = encode_checked
    try:
        _transcribe_file(model, audio_path, txt_path, config, hooks)
    finally:
        try:
            del model.encode  # back to the normal method
        except AttributeError:
            pass


def _transcribe_file(model, audio_path: Path, txt_path: Path, config: dict,
                     hooks: Hooks = NO_HOOKS) -> None:
    """Transcribe one audio/video file and write the .txt file.

    The text is first written to a temporary ".part" file and renamed at the
    end. That way a half-finished transcript (e.g. if the program is closed)
    never looks like a finished one.
    """
    hint = filename_hint(audio_path) if config["use_filename_hint"] else None
    if hint:
        log.info('   Using hint from file name: "%s"', hint)
    # The silence filter drops most singing, so with music labels on (the
    # songs are wanted in the transcript) it's not used. Instead only true
    # dead air is skipped and marked [SILENCE ...].
    use_vad = config["vad_filter"] and not config.get("label_music")
    source, music, gaps, clips = str(audio_path), None, [], "0"
    if config.get("label_music"):
        import music_detect
        from faster_whisper.audio import decode_audio
        source = decode_audio(str(audio_path))  # decoded once, used for all steps
        duration = len(source) / music_detect.SAMPLE_RATE
        music = find_music(source, hooks)
        gaps = music_detect.find_silence(source)
        if gaps:
            silent = sum(b - a for a, b in gaps)
            log.info("   Dead air: %d stretch(es), %s in total (skipped)",
                     len(gaps), format_timestamp(silent))
            clips = music_detect.sound_clips(gaps, duration)

    if not clips:  # the whole file is silent
        segments, info = iter(()), None
    else:
        segments, info = model.transcribe(
            source,
            clip_timestamps=clips,
            language=config["language"],
            vad_filter=use_vad,  # skip silence
            hotwords=hint,
            beam_size=SPEED_BEAM_SIZE[config["speed"]],
            # Check several 30s chunks when detecting the language, not just the
            # first one (song intros are often instrumental).
            language_detection_segments=4,
            # Don't feed each 30-second chunk the text of the previous one.
            # On radio recordings that made Whisper get stuck repeating a
            # lyric and then write nothing for minutes of clear speech.
            condition_on_previous_text=False,
        )

    if info is None:
        log.info("   The whole file is silent.")
    elif config["language"] is None:
        log.info("   Detected language: %s (%.0f%% sure), audio length %s",
                 info.language, info.language_probability * 100,
                 format_timestamp(info.duration))

    # The silence filter can mistake singing over music for "no speech".
    # Warn if it threw away most of the file.
    kept = getattr(info, "duration_after_vad", None) if info else None
    if use_vad and kept is not None and info.duration > 30 and kept < info.duration * 0.5:
        log.warning("   Silence filter kept only %s of %s. If this is music, turn off "
                    '"Skip silence" (or set "vad_filter": false in config.json).',
                    format_timestamp(kept), format_timestamp(info.duration))

    lines = []
    pending_gaps = list(gaps)

    def add_gaps(before):
        while pending_gaps and pending_gaps[0][0] <= before:
            a, b = pending_gaps.pop(0)
            lines.append(f"[SILENCE {format_timestamp(a)} → {format_timestamp(b)}]")

    last_report = time.time()
    hooks.progress(audio_path, 0)
    for seg in segments:  # segments are produced as the audio is processed
        hooks.checkpoint()  # pausing here simply holds the work until resumed
        if info.duration:
            hooks.progress(audio_path, min(100.0, seg.end / info.duration * 100))
        text = seg.text.strip()
        if not text or is_hallucination(text):
            continue
        add_gaps(seg.start)
        tag = "MUSIC " if music is not None and music_detect.is_music(
            music, seg.start, seg.end) else ""
        if config["include_timestamps"]:
            lines.append(f"[{tag}{format_timestamp(seg.start)} → "
                         f"{format_timestamp(seg.end)}] {text}")
        elif tag:
            lines.append(f"[MUSIC] {text}")
        else:
            lines.append(text)

        # Show a progress percentage every ~15 seconds for long files.
        if info.duration and time.time() - last_report > 15:
            pct = min(100, seg.end / info.duration * 100)
            log.info("   ... %d%%", pct)
            last_report = time.time()

    add_gaps(float("inf"))

    # Unique temp name per PC and process, so two PCs can never write into
    # the same temp file on a shared folder.
    tmp_path = txt_path.with_name(f"{txt_path.name}.{HOSTNAME}-{os.getpid()}.part")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
            f.write("\n")
        os.replace(tmp_path, txt_path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

def list_audio_files(input_dir: Path, file_types: set) -> list:
    """Audio/video files directly in input_dir (no subfolders), sorted by name."""
    if not input_dir.is_dir():
        return []
    files = [p for p in input_dir.iterdir()
             if p.is_file() and p.suffix.lower() in file_types]
    return sorted(files, key=lambda p: p.name.lower())


def arrival_time(path: Path) -> float:
    """When the file appeared in the folder.

    Uses the later of "created" and "modified": a copied file keeps its old
    modified date but gets a new created date, while a file that is still
    being recorded keeps getting a newer modified date.
    """
    try:
        st = path.stat()
    except OSError:
        return 0.0
    created = getattr(st, "st_birthtime", st.st_ctime)  # creation time on Windows
    return max(created, st.st_mtime)


def sort_queue(files: list, order: str) -> list:
    """Order files for processing according to the "order" setting."""
    if order == "oldest_first":
        return sorted(files, key=arrival_time)
    if order == "name":
        return sorted(files, key=lambda p: p.name.lower())
    return sorted(files, key=arrival_time, reverse=True)  # newest_first


def hide_file(path: Path) -> None:
    """Mark a file as hidden in Explorer (Windows only, best effort)."""
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 2
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


class OutputNames:
    """Decides the .txt name for each input file, and remembers it.

    Normally song.mp3 -> song.txt. But if song.txt was already made from a
    different file with the same name (e.g. song.mp4), the new one becomes
    song.mp3.txt, so neither file blocks or overwrites the other.

    Which input each .txt came from is kept in a small hidden file in the
    output folder (.transcriber-index.json).
    """

    INDEX_NAME = ".transcriber-index.json"

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.index_path = output_dir / self.INDEX_NAME
        self.owners = {}  # "song.txt" (lowercase) -> "song.mp3"
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self.owners = {k.lower(): v for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            pass  # no index yet (or unreadable): start fresh

    def path_for(self, audio: Path) -> Path:
        plain = audio.stem + ".txt"
        owner = self.owners.get(plain.lower())
        if owner is None or owner.lower() == audio.name.lower():
            return self.output_dir / plain
        return self.output_dir / (audio.name + ".txt")  # song.mp4.txt

    def record(self, txt: Path, audio: Path) -> None:
        """Record that txt was made from audio."""
        if self.owners.get(txt.name.lower()) == audio.name:
            return
        self.owners[txt.name.lower()] = audio.name
        try:
            # Other PCs may share this output folder: merge in what they
            # recorded since we last read the index, so nothing is lost.
            try:
                on_disk = json.loads(self.index_path.read_text(encoding="utf-8"))
                for k, v in on_disk.items():
                    self.owners.setdefault(k.lower(), v)
            except (OSError, ValueError, AttributeError):
                pass
            tmp = self.index_path.with_name(f"{self.INDEX_NAME}.{HOSTNAME}-{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.owners, indent=1), encoding="utf-8")
            os.replace(tmp, self.index_path)
            hide_file(self.index_path)
        except OSError as e:
            log.warning("Could not save %s: %s", self.index_path.name, e)


class WorkClaim:
    """Marks a file as "being transcribed" so other PCs leave it alone.

    Several PCs can watch the same shared folder. Before starting a file, a PC
    creates "<transcript>.inprogress" in the output folder. Creating it is
    atomic, so if two PCs try at the same moment only one succeeds. While
    working, the owner touches the file every 30 seconds. A claim that nobody
    has touched for 10 minutes (that PC crashed, lost power or was closed)
    is taken over by another PC.
    """

    SUFFIX = ".inprogress"
    HEARTBEAT_SECONDS = 30
    STALE_SECONDS = 10 * 60

    def __init__(self, txt: Path):
        self.path = txt.with_name(txt.name + self.SUFFIX)
        self._stop = None

    def acquire(self) -> bool:
        """Try to claim the file. False if another PC already has it."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{HOSTNAME}\n{os.getpid()}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        hide_file(self.path)
        # Keep the claim "alive" from a background thread, so it stays fresh
        # even while the model is busy with a long stretch of audio.
        import threading
        self._stop = threading.Event()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        return True

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.HEARTBEAT_SECONDS):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self) -> None:
        if self._stop:
            self._stop.set()
        try:
            self.path.unlink()
        except OSError:
            pass

    @staticmethod
    def owner_of(path: Path):
        """(hostname, pid) written in a claim file, or None."""
        try:
            host, pid = path.read_text(encoding="utf-8").splitlines()[:2]
            return host, int(pid)
        except (OSError, ValueError):
            return None


def file_signature(path: Path):
    """(size, modified time) - used to notice if a failed file was replaced."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def get_size(path: Path):
    try:
        return path.stat().st_size
    except OSError:
        return None


def can_open(path: Path) -> bool:
    """False if Windows still has the file locked (e.g. mid-copy)."""
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


class Transcriber:
    def __init__(self, model, config: dict, hooks: Hooks = NO_HOOKS, failed=None):
        self.model = model
        self.hooks = hooks
        self.config = config
        self.input_dir = Path(config["input_folder"])
        self.output_dir = Path(config["output_folder"])
        self.names = OutputNames(self.output_dir)
        # Files that failed: name -> signature. Not retried unless the file changes.
        # (Passed in by the window, so skipped files stay skipped after a
        # settings change restarts this loop.)
        self.failed = {} if failed is None else failed
        # Last seen size of files not done yet: name -> size. A file is only
        # started once its size is the same in two scans in a row (so files
        # still being copied or recorded are left alone).
        self.pending_sizes = {}
        # Other PCs' claims we've seen: claim path -> (modified time, when we
        # first saw that modified time). Used to spot abandoned claims.
        self.claims_seen = {}
        self.started_scanning = False

    def is_claimed_by_other_pc(self, txt: Path) -> bool:
        """True if another PC is working on this transcript right now."""
        claim = txt.with_name(txt.name + WorkClaim.SUFFIX)
        try:
            mtime = claim.stat().st_mtime
        except OSError:
            self.claims_seen.pop(claim, None)
            return False

        owner = WorkClaim.owner_of(claim)
        if owner and owner[0] == HOSTNAME and owner[1] != os.getpid():
            # Left behind by an earlier run on this PC (closed or crashed);
            # only one copy runs per PC, so it's safe to take over now.
            log.info("Resuming %s (left unfinished by an earlier run)", txt.name)
            self._remove_claim(claim)
            return False

        # A claim counts as abandoned if its time hasn't changed for
        # STALE_SECONDS of our own clock (so PC clock differences don't matter).
        now = time.time()
        seen = self.claims_seen.get(claim)
        if seen is None or seen[0] != mtime:
            self.claims_seen[claim] = (mtime, now)
            return True
        if now - seen[1] > WorkClaim.STALE_SECONDS:
            log.warning("Taking over %s: %s stopped working on it",
                        txt.name, owner[0] if owner else "another PC")
            self._remove_claim(claim)
            return False
        return True

    def _remove_claim(self, claim: Path) -> None:
        self.claims_seen.pop(claim, None)
        try:
            claim.unlink()
        except OSError:
            pass

    def needs_work(self, audio: Path) -> bool:
        """True if this file has no .txt yet, isn't being done by another
        PC, and hasn't already failed here."""
        txt = self.names.path_for(audio)
        if txt.exists():
            # Transcripts made before the index existed: record who owns them,
            # so a later file with the same name gets its own .txt.
            self.names.record(txt, audio)
            return False
        if audio.name in self.failed and self.failed[audio.name] == file_signature(audio):
            return False
        if self.is_claimed_by_other_pc(txt):
            return False
        return True

    def scan(self):
        """Look at the input folder once.

        Returns (ready, waiting, saw_new_file): files ready to transcribe in
        queue order, all files still to do, and whether a file was seen for
        the first time (so we re-check it soon).
        """
        all_files = list_audio_files(self.input_dir, self.config["file_types"])
        waiting = [p for p in all_files if self.needs_work(p)]
        waiting_names = {p.name for p in waiting}

        if not self.started_scanning:
            done = len(all_files) - len(waiting)
            log.info("Found %d audio file%s (%d already transcribed, %d to do)",
                     len(all_files), "" if len(all_files) == 1 else "s", done, len(waiting))

        # Forget files that disappeared or got done.
        for name in list(self.pending_sizes):
            if name not in waiting_names:
                del self.pending_sizes[name]

        ready, saw_new_file = [], False
        for p in waiting:
            size = get_size(p)
            previous = self.pending_sizes.get(p.name)
            self.pending_sizes[p.name] = size
            if size and size == previous and can_open(p):
                ready.append(p)
            elif previous is None:
                saw_new_file = True
                if self.started_scanning:
                    log.info("New file: %s (waiting for copy to finish)", p.name)

        self.started_scanning = True
        return sort_queue(ready, self.config["order"]), waiting, saw_new_file

    def process_one(self, audio: Path, still_waiting: int) -> None:
        """Transcribe one file. A failure is logged and never stops the program."""
        txt = self.names.path_for(audio)
        claim = WorkClaim(txt)
        try:
            got_it = claim.acquire()
        except OSError as e:
            # e.g. no write permission in the output folder. Treat it as a
            # failure so we don't retry it in a tight loop.
            log.error("FAILED %s: could not create %s (%s)", audio.name, claim.path.name, e)
            self.failed[audio.name] = file_signature(audio)
            return
        if not got_it:
            return  # another PC grabbed it a moment ago
        try:
            if txt.exists():  # another PC finished it a moment ago
                self.names.record(txt, audio)
                return
            more = f" ({still_waiting} more waiting)" if still_waiting else ""
            log.info("Transcribing %s...%s", audio.name, more)
            self.hooks.event("started", audio=audio, waiting=still_waiting)
            start = time.time()
            transcribe_file(self.model, audio, txt, self.config, self.hooks)
            self.names.record(txt, audio)
            took = time.time() - start
            log.info("Done → %s (took %.0fs)", txt.name, took)
            self.hooks.event("done", audio=audio, txt=txt, seconds=took)
            self.failed.pop(audio.name, None)
        except (KeyboardInterrupt, Stopped):
            raise
        except Skipped:
            # Not retried unless the file changes or the program restarts.
            log.info("Skipped: %s", audio.name)
            self.failed[audio.name] = file_signature(audio)
            self.hooks.event("skipped", audio=audio)
        except Exception as e:
            log.error("FAILED %s: %s (skipping it)", audio.name, e)
            file_only.error("Details for %s:\n%s", audio.name, traceback.format_exc())
            self.failed[audio.name] = file_signature(audio)
            self.hooks.event("failed", audio=audio, error=str(e))
        finally:
            claim.release()

    def run_forever(self) -> None:
        """Main loop: always transcribe the next file in queue order (newest
        first by default), re-checking the folder between files so a newly
        arrived file doesn't wait behind a long backlog."""
        interval = self.config["poll_interval_seconds"]
        hooks = self.hooks
        idle = False
        while not hooks.should_restart():
            hooks.checkpoint()
            try:
                ready, waiting, saw_new_file = self.scan()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # e.g. a network folder is temporarily unavailable - try again.
                log.error("Error while checking folder: %s", e)
                hooks.sleep(interval)
                continue
            hooks.event("queue", waiting=len(waiting),
                        files=sort_queue(waiting, self.config["order"]))

            if saw_new_file:
                # A file seen for the first time gets a second look in 2
                # seconds (to confirm it has finished copying) BEFORE we start
                # anything else, so a fresh file isn't stuck behind one more
                # long backlog file.
                hooks.sleep(2)
                continue

            if ready:
                idle = False
                self.process_one(hooks.pick(ready), len(waiting) - 1)
                continue

            if not idle and not waiting:
                log.info("All caught up. Watching %s for new audio files every %ds...",
                         self.input_dir, interval)
                hooks.event("idle")
                idle = True
            hooks.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    log.info("=" * 60)
    log.info("%s v%s starting", APP_NAME, __version__)

    config = load_config()
    input_dir = Path(config["input_folder"])
    output_dir = Path(config["output_folder"])
    ensure_single_instance(input_dir)

    log.info("Input folder : %s", input_dir)
    log.info("Output folder: %s", output_dir)
    log.info("Model: %s (%s) | Language: %s | Timestamps: %s | Silence filter: %s",
             config["model_size"], config["speed"], config["language"] or "auto-detect",
             "on" if config["include_timestamps"] else "off",
             "on" if config["vad_filter"] else "off")
    log.info("Queue order: %s | This PC: %s", config["order"].replace("_", " "), HOSTNAME)

    # Create both folders if needed, so first-time users get a folder ready
    # to drop files into.
    for folder in (input_dir, output_dir):
        if not folder.is_dir():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                log.info("Created folder: %s", folder)
            except OSError as e:
                raise UserError(
                    f"Could not create the folder {folder} ({e}).\n"
                    f"Edit input_folder / output_folder in {CONFIG_PATH}"
                ) from None

    model = load_model(config)
    Transcriber(model, config).run_forever()


def folder_key(input_dir: Path) -> str:
    """Short id for an input folder (used to name the single-instance lock)."""
    return hashlib.sha1(str(input_dir.resolve()).lower().encode("utf-8")).hexdigest()[:16]


# Kept for the whole run: Windows releases the lock when the program exits.
_instance_lock = None


def ensure_single_instance(input_dir: Path) -> None:
    """Stop if another copy is already watching the same input folder.

    Two copies would grab the same new files and transcribe them twice.
    Uses a named Windows mutex, which disappears automatically when the
    program closes or crashes, so it can never get "stuck".
    """
    global _instance_lock
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    folder_id = folder_key(input_dir)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    handle = kernel32.CreateMutexW(None, False, f"Local\\AudioTranscriber-{folder_id}")
    if not handle:
        return  # couldn't check; carry on rather than block the user
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise UserError(
            f"Another copy of the transcriber is already watching {input_dir}.\n"
            "Close the other window first (or check the taskbar and the tray "
            "icons next to the clock), then start this one again."
        )
    if _instance_lock:  # switching to a different input folder
        kernel32.CloseHandle(_instance_lock)
    _instance_lock = handle


def wait_for_enter() -> None:
    try:
        input("Press Enter to exit...")
    except (EOFError, KeyboardInterrupt):
        pass


def cli() -> None:
    """Run in a console window (the original text-only mode)."""
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except UserError as e:
        # A problem the user can fix: show just the message.
        print("\n" + "=" * 60)
        print(e)
        print("=" * 60)
        file_only.error("Stopped: %s", e)
        wait_for_enter()
        sys.exit(1)
    except Exception:
        # Unexpected error: show the full details instead of letting the
        # window vanish.
        print("\n" + "=" * 60)
        print("ERROR - the program stopped:")
        traceback.print_exc()
        print("=" * 60)
        if file_only.handlers:
            file_only.error("FATAL ERROR:\n%s", traceback.format_exc())
        else:  # logging wasn't set up yet
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write("FATAL ERROR:\n" + traceback.format_exc())
            except OSError:
                pass
        wait_for_enter()
        sys.exit(1)


if __name__ == "__main__":
    cli()
