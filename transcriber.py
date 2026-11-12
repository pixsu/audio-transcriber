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
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import time

try:
    import numpy as np
except ImportError:  # only needed for the music / dead-air features
    np = None
import traceback
from pathlib import Path

# Bump this for each release (and add an entry to CHANGELOG.md). The release
# build checks that the git tag (e.g. v1.0.0) matches it.
__version__ = "1.29.2"
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
    "include_timestamps": True,
    # Put [MUSIC] in front of lines where music or singing dominates (songs,
    # jingles). Everything is still transcribed.
    "label_music": True,
    # Names to spell right and fixes ("wrong" -> "right") are kept in
    # .vocabulary.json in the Transcripts folder, shared by every PC using it.
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
    # Always "accurate" (compares 5 guesses at each step). Not shown in the
    # window; "fast" (one guess, ~35% faster, worse on music and noisy
    # audio) can still be set here by hand for a very slow PC.
    "speed": "accurate",
    # Bumped when a stored setting needs a one-time change on update.
    "settings_version": 2,
    "poll_interval_seconds": 10,
    # "auto" = GPU if available, else CPU. Or force "cuda" or "cpu".
    "device": "auto",
    # Every hour, check GitHub for a newer version and show a notice.
    "check_for_updates": True,
}

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"}
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


# --- shared vocabulary ------------------------------------------------------
# Names and fixes live in a hidden file in the Transcripts folder, so every PC
# that shares that folder uses (and adds to) the same lists.
VOCAB_FILE = ".vocabulary.json"


def _clean_words(words) -> list:
    if isinstance(words, str):
        words = words.replace("\n", ",").split(",")
    out, seen = [], set()
    for w in words or []:
        w = str(w).strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def _clean_fixes(fixes) -> dict:
    if not isinstance(fixes, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in fixes.items() if str(k).strip()}


class VocabularyUnreadable(OSError):
    """The shared vocabulary exists but couldn't be read right now (another
    PC is saving it, or the network blinked). Nothing must be written then,
    or every PC's names and fixes would be lost."""


def read_vocabulary(output_folder, strict: bool = False) -> dict:
    """{"word_list": [...], "replacements": {...}} from the shared file.

    A file that doesn't exist (yet) is empty. Any other problem is retried a
    few times; then strict=True raises VocabularyUnreadable, and
    strict=False (for showing counts) returns empty lists."""
    path = Path(output_folder) / VOCAB_FILE
    last_error = None
    for attempt in range(5):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a vocabulary file")
            return {"word_list": _clean_words(data.get("word_list")),
                    "replacements": _clean_fixes(data.get("replacements"))}
        except FileNotFoundError:
            return {"word_list": [], "replacements": {}}
        except (OSError, ValueError) as e:
            last_error = e
            time.sleep(0.3 * (attempt + 1))
    if strict:
        raise VocabularyUnreadable(f"the shared names and fixes couldn't be read ({last_error})")
    log.warning("Couldn't read the shared names and fixes (%s)", last_error)
    return {"word_list": [], "replacements": {}}


def write_vocabulary(output_folder, word_list, replacements) -> None:
    """Save the shared lists (temp file + rename, so it's never half-written).
    Use update_vocabulary() to change them: it re-reads first, under a lock."""
    folder = Path(output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / VOCAB_FILE
    tmp = path.with_name(f"{VOCAB_FILE}.{HOSTNAME}-{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"word_list": _clean_words(word_list),
                   "replacements": _clean_fixes(replacements)},
                  f, ensure_ascii=False, indent=1)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # another PC is reading it this instant
            time.sleep(0.3 * (attempt + 1))
    os.replace(tmp, path)


VOCAB_LOCK = ".vocabulary.lock"


def update_vocabulary(output_folder, change) -> dict:
    """Change the shared lists safely: take a short lock (so two PCs take
    turns), read the latest version (refusing if it can't be read), apply
    change(vocab) and save. Returns the saved lists."""
    folder = Path(output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / VOCAB_LOCK
    got = False
    for _ in range(50):  # up to ~10 s
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{HOSTNAME} {os.getpid()}".encode())
            os.close(fd)
            got = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:  # left by a crash
                    os.rename(lock, lock.with_name(f"{VOCAB_LOCK}.stale-{HOSTNAME}-{os.getpid()}"))
                    continue
            except OSError:
                pass
            time.sleep(0.2)
    if not got:
        raise VocabularyUnreadable("another PC is busy saving the names and fixes; try again")
    try:
        vocab = read_vocabulary(folder, strict=True)
        change(vocab)
        write_vocabulary(folder, vocab["word_list"], vocab["replacements"])
        return vocab
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
        for stale in folder.glob(f"{VOCAB_LOCK}.stale-*"):
            try:
                stale.unlink()
            except OSError:
                pass


def add_fix(output_folder, wrong: str, right: str) -> None:
    """Add one fix to the latest shared list."""
    update_vocabulary(output_folder, lambda v: v["replacements"].__setitem__(wrong, right))


def save_vocabulary_edits(output_folder, base: dict, words: list, fixes: dict) -> dict:
    """Apply what the user changed in the Vocabulary window (compared with
    `base`, what it showed when opened) on top of the latest shared lists,
    so names and fixes another PC added meanwhile are kept."""
    base_words = {w.lower() for w in base["word_list"]}
    new_words = {w.lower() for w in words}
    removed_words = base_words - new_words
    removed_fixes = set(base["replacements"]) - set(fixes)
    changed = {k: v for k, v in fixes.items() if base["replacements"].get(k) != v}

    def apply(v):
        kept = [w for w in v["word_list"] if w.lower() not in removed_words]
        v["word_list"] = _clean_words(kept + [w for w in words if w.lower() not in base_words])
        for k in removed_fixes:
            v["replacements"].pop(k, None)
        v["replacements"].update(changed)
    return update_vocabulary(output_folder, apply)


def move_vocabulary_to_shared(raw: dict) -> None:
    """Older versions kept names and fixes in this PC's config.json. Merge
    them into the shared file once and remove them from config.json."""
    words = _clean_words(raw.get("word_list"))
    fixes = _clean_fixes(raw.get("replacements"))
    if not words and not fixes:
        return
    folder = raw.get("output_folder") or DEFAULT_CONFIG["output_folder"]

    def merge(shared):
        shared["word_list"] = _clean_words(shared["word_list"] + words)
        merged = dict(fixes)
        merged.update(shared["replacements"])  # the shared list wins on clashes
        shared["replacements"] = merged
    try:
        update_vocabulary(folder, merge)
    except OSError as e:
        log.warning("Couldn't move names and fixes to the shared Transcripts folder yet "
                    "(%s); will try again next time.", e)
        return
    raw = dict(raw)
    raw.pop("word_list", None)
    raw.pop("replacements", None)
    save_raw_config(raw)
    log.info("Moved %d name(s) and %d fix(es) into the shared vocabulary in %s",
             len(words), len(fixes), folder)


def load_config() -> dict:
    """Load config.json, creating it with defaults if it doesn't exist.

    Missing keys are filled in from DEFAULT_CONFIG, so an old config file
    keeps working after new settings are added.
    """
    user_config = read_raw_config()
    if int(user_config.get("settings_version") or 1) < 2:
        # v1.20: Fast mode was removed from the window; move everyone to
        # Accurate once (a hand-set "fast" afterwards is respected).
        user_config["speed"] = "accurate"
        user_config["settings_version"] = 2
        save_raw_config(user_config)
        log.info("Mode set to Accurate (the Fast option has been retired).")
    if user_config.get("word_list") or user_config.get("replacements"):
        move_vocabulary_to_shared(user_config)
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
    # Names and fixes come from the shared file in the Transcripts folder.
    # If this PC's old lists couldn't be moved there yet (folder not
    # reachable), keep using them meanwhile.
    shared = read_vocabulary(config["output_folder"])
    local_fixes = _clean_fixes(user_config.get("replacements"))
    local_fixes.update(shared["replacements"])
    config["word_list"] = _clean_words(shared["word_list"] + _clean_words(user_config.get("word_list")))
    config["replacements"] = local_fixes
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


def build_hotwords(hint, words):
    """File-name hint plus the word list, kept short (Whisper only reads
    about 220 tokens of hints)."""
    parts = ([hint] if hint else []) + list(words)
    text = ""
    for part in parts:
        if len(text) + len(part) + 2 > 400:
            break
        text = f"{text}, {part}" if text else part
    return text or None


def make_replacer(replacements: dict):
    """A function that applies the find-and-replace list to one line."""
    if not replacements:
        return lambda text: text
    # Longest first, so "Love Radio Naga" wins over "Love Radio".
    def style(word):
        if word.isupper():
            return "upper"
        if word[:1].isupper() and word[1:] == word[1:].lower():
            return "title"
        return "lower" if word.islower() else "mixed"

    def matched_case(found, wrong, right):
        # "Tuyoy = Tuloy" (same capitals on both sides) is an ordinary word:
        # follow the transcript, so "tuyoy" -> "tuloy", "TUYOY" -> "TULOY".
        # Anything else ("Love rate = Love Radio") is written exactly as typed.
        if style(wrong) != style(right) or style(right) == "mixed":
            return right
        kind = style(found)
        if kind == "upper":
            return right.upper()
        if kind == "lower":
            return right.lower()
        if kind == "title":
            return right[:1].upper() + right[1:].lower()
        return right

    rules = [(re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", re.IGNORECASE), wrong, right)
             for wrong, right in sorted(replacements.items(), key=lambda kv: -len(kv[0]))]

    def apply(text):
        for pattern, wrong, right in rules:
            text = pattern.sub(lambda m: matched_case(m.group(0), wrong, right), text)
        return text
    return apply


# Languages written in the Latin alphabet. When one of these is chosen,
# Whisper's occasional stray Korean / Chinese / Russian letters (invented
# during music) are removed.
LATIN_LANGUAGES = {"tl", "en", "es", "id", "ms"}


def strip_foreign_script(text: str) -> str:
    kept = "".join(ch for ch in text if not (ch.isalpha() and ord(ch) > 0x24F))
    return " ".join(kept.split())


# Short lines Whisper tends to invent over instrumental music. Dropped only
# when they come from the second pass (which re-reads music-only stretches).
FILLER_LINES = {"thank you", "thanks", "thank you very much", "thank you so much",
                "you", "bye", "bye bye", "thank you bye",
                "terima kasih", "terima kasih banyak",  # Malay "thank you", also invented
                "music", "intro music",
                "outro music", "background music", "instrumental", "applause"}


# Never real content in a transcript: Whisper writes these as captions.
CAPTION_WORDS = {"outro", "intro", "music", "intro music", "outro music",
                 "background music", "instrumental", "instrumental music"}


def is_caption(text: str) -> bool:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split()) in CAPTION_WORDS


def is_filler(text: str) -> bool:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split()) in FILLER_LINES


def is_stuck_loop(text: str) -> bool:
    """Whisper stuck repeating itself: 'Pag-ag-ag-ag-ag...' or 'pag-uswag na
    pag-uswag na pag-uswag na ...' (a word or short phrase, 6+ times in a row,
    making up most of the line)."""
    words = re.findall(r"\w+", text.lower())
    if len(words) >= 6:
        for size in range(1, 5):  # phrases of 1-4 words
            run, best = 1, 1
            for i in range(size, len(words) - size + 1, size):
                if words[i:i + size] == words[i - size:i]:
                    run += 1
                    best = max(best, run)
                else:
                    run = 1
            if best >= 6 and best * size >= len(words) * 0.5:
                return True
    m = max((len(x.group(0)) for x in re.finditer(r"(\w{1,6})(?:[\s,\-]+\1){5,}",
                                                    text, re.IGNORECASE)), default=0)
    return m > len(text) * 0.5


def shorten_loop(text: str) -> str:
    """'pag-uswag na pag-uswag na (x40)' -> 'pag-uswag na, pag-uswag na,
    pag-uswag na…': the longest run of a repeated 1-4 word phrase is cut
    down to three times."""
    words = re.findall(r"[\w'\-]+", text)
    low = [w.lower() for w in words]
    best = None  # (covered words, start, size, repeats)
    for size in range(1, 5):
        i = 0
        while i + size <= len(low):
            reps = 1
            while low[i + reps * size:i + (reps + 1) * size] == low[i:i + size]:
                reps += 1
            if reps >= 4 and (best is None or reps * size > best[0]):
                best = (reps * size, i, size, reps)
            i += 1
    if not best:
        return text
    _, i, size, reps = best
    phrase = " ".join(words[i:i + size])
    before = " ".join(words[:i])
    after = " ".join(words[i + reps * size:])
    out = ", ".join([phrase] * 3) + "…"
    return " ".join(p for p in (before, out, after) if p)


FILLER_NEIGHBOUR_SECONDS = 5.0  # real speech this close keeps a "Thank you"


def drop_invented_fillers(found, music=None):
    """Remove short filler lines ("Thank you.", "you") that Whisper invents
    over music or in otherwise empty stretches. One is kept when it's not
    over music and other speech is close by: a DJ really saying "thank you"
    in a conversation. found: sorted [(start, end, text)]."""
    import music_detect
    real = [(a, b) for a, b, t in found if not is_filler(t)]
    kept, dropped = [], []
    for a, b, text in found:
        if is_filler(text):
            over_music = music is not None and (
                music_detect.is_music(music, a - 1, b + 1)
                or music_detect.describe_empty(music, a - 1, b + 1) == "(music, no clear words)")
            near = any(ra - FILLER_NEIGHBOUR_SECONDS <= b and rb + FILLER_NEIGHBOUR_SECONDS >= a
                       for ra, rb in real)
            if over_music or not near:
                dropped.append((a, b, text))
                continue
        kept.append((a, b, text))
    return kept, dropped


REPEAT_GAP_SECONDS = 3.0


def merge_repeated_lines(found):
    """3+ identical lines in a row (Whisper stuck, or a chant) become one
    line covering all of them: "pangalang (×5)". found: sorted
    [(start, end, text)]. Returns (lines, how many were merged away)."""
    def norm(t):
        return " ".join(re.sub(r"[^\w\s]", "", t.lower()).split())
    out, merged, i = [], 0, 0
    while i < len(found):
        a, b, text = found[i]
        j = i + 1
        while (j < len(found) and norm(found[j][2]) == norm(text) and norm(text)
               and found[j][0] - found[j - 1][1] <= REPEAT_GAP_SECONDS):
            j += 1
        count = j - i
        if count >= 3:
            out.append((a, found[j - 1][1], f"{text.rstrip(' .,')} (×{count})"))
            merged += count - 1
        else:
            out.extend(found[i:j])
        i = j
    return out, merged


def is_hallucination(text: str) -> bool:
    norm = " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())
    return norm in HALLUCINATIONS or norm.startswith("subtitles by") or "amara org" in norm


HOLE_MIN_SECONDS = 6.0


def merge_spans(spans):
    """Sorted, non-overlapping version of a list of (start, end)."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
GAP_LABEL_SECONDS = 3.0  # shorter pauses are joined onto the line before


def find_holes(found, clips, min_seconds=None):
    """Stretches of at least HOLE_MIN_SECONDS inside the parts with sound
    (clips: [start, end, start, end, ...]) that got no text."""
    min_seconds = HOLE_MIN_SECONDS if min_seconds is None else min_seconds
    holes = []
    pairs = list(zip(clips[0::2], clips[1::2]))
    for a, b in pairs:
        inside = sorted((s, e) for s, e, _ in found if e > a and s < b)
        pos = a
        for s, e in inside + [(b, b)]:
            if s - pos >= min_seconds:
                holes.append((round(pos, 2), round(s, 2)))
            pos = max(pos, e)
    return holes


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
    try:  # the latest shared names and fixes (another PC may have added some)
        config = dict(config, **read_vocabulary(config["output_folder"], strict=True))
    except OSError as e:
        log.warning("   Using the names and fixes loaded earlier (%s)", e)
    hint = filename_hint(audio_path) if config["use_filename_hint"] else None
    if hint:
        log.info('   Using hint from file name: "%s"', hint)
    # The silence filter drops most singing, so with music labels on (the
    # songs are wanted in the transcript) it's not used. Instead only true
    # dead air is skipped and marked [SILENCE ...].
    use_vad = config["vad_filter"] and not config.get("label_music")
    source, music, gaps, clips = str(audio_path), None, [], "0"
    jingle_hits = {}  # (start, end) -> (label, text), from the jingle/song library
    file_codes = None  # sound codes of this recording (worked out once, used twice)
    library = []
    try:
        import jingles
        library = jingles.load_library(config["output_folder"])
    except Exception as e:
        log.warning("   Jingle library couldn't be read (%s)", e)
    if config.get("label_music") or library:
        import music_detect
        from faster_whisper.audio import decode_audio
        source = decode_audio(str(audio_path))  # decoded once, used for all steps
        duration = len(source) / music_detect.SAMPLE_RATE
        skip = []
        if config.get("label_music"):
            music = find_music(source, hooks)
            gaps = music_detect.find_silence(source)
            if gaps:
                silent = sum(b - a for a, b in gaps)
                log.info("   Dead air: %d stretch(es), %s in total (skipped)",
                         len(gaps), format_timestamp(silent))
            skip += gaps
        if library:
            try:
                names = []
                file_codes = jingles.make_codes(source)
                for a, b, item, offset in jingles.find_jingles_fast(source, library, file_codes,
                                                                    hooks.checkpoint):
                    a, b = max(0.0, a), min(duration, b)
                    if any(b > ga and a < gb for ga, gb in gaps):
                        continue  # never over dead air
                    for la, lb, text, label in jingles.lines_for_match(item, a, b, offset):
                        la, lb = max(a, la), min(b, lb)
                        if lb > la:
                            jingle_hits[(la, lb)] = (label, text)
                    skip.append((a, b))
                    names.append(item.get("name", "?"))
                if names:
                    log.info("   Recognised from the library (not transcribed): %s",
                             ", ".join(names))
            except (Stopped, Skipped):
                raise
            except Exception as e:
                log.warning("   Jingle matching didn't work (%s)", e)
        if skip:
            clips = music_detect.sound_clips(merge_spans(skip), duration)
        # Keep this recording's sound codes, so Find repeats can compare it
        # with every other recording later (even once the original is gone).
        try:
            import jingles
            jingles.store_codes(config["output_folder"], audio_path, source,
                                codes=file_codes if file_codes is not None else None)
        except Exception as e:
            log.debug("   Couldn't store sound codes (%s)", e)

    options = dict(
        language=config["language"],
            vad_filter=use_vad,  # skip silence
            hotwords=build_hotwords(hint, config.get("word_list", [])),
            beam_size=SPEED_BEAM_SIZE[config["speed"]],
            # Check several 30s chunks when detecting the language, not just the
            # first one (song intros are often instrumental).
            language_detection_segments=4,
            # Don't feed each 30-second chunk the text of the previous one.
            # On radio recordings that made Whisper get stuck repeating a
            # lyric and then write nothing for minutes of clear speech.
            condition_on_previous_text=False,
            # Whisper drops a whole 30-second chunk when it "sounds like no
            # speech", which randomly hit chanted jingles and talk over music.
            # With music labels on, real silence is already found and skipped
            # by the dead-air check, so keep everything else.
            no_speech_threshold=None if config.get("label_music") else 0.6,
            # Times from the words themselves, not Whisper's rough guess per
            # line: a line no longer "starts" at the beginning of a song
            # intro or ends 20 seconds after the last word.
            word_timestamps=True,
    )
    # Whisper retries hard parts with some randomness. A fixed starting
    # point makes a file come out the same every time it's transcribed.
    try:
        import ctranslate2
        ctranslate2.set_random_seed(1234)
    except Exception:
        pass
    if not clips:  # the whole file is silent
        segments, info = iter(()), None
    else:
        segments, info = model.transcribe(source, clip_timestamps=clips, **options)

    if info is None:
        log.info("   Nothing left to transcribe (silence or recognised jingles).")
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
    fix_words = make_replacer(config.get("replacements", {}))
    latin_only = config["language"] in LATIN_LANGUAGES

    def clean(seg):
        text = fix_words(seg.text.strip())
        if latin_only:
            text = strip_foreign_script(text)
        if text and is_stuck_loop(text):
            text = shorten_loop(text)
            if is_stuck_loop(text):  # couldn't be tidied: drop it
                return None
        if not re.search(r"\w", text or ""):
            return None  # only dots or dashes ("...", "…"): no words at all
        return None if is_hallucination(text) else text

    found = []  # (start, end, text)
    last_report = time.time()
    hooks.progress(audio_path, 0)
    refill = isinstance(source, np.ndarray) if np is not None else False
    first_share = 90.0 if refill else 100.0
    for seg in segments:  # segments are produced as the audio is processed
        hooks.checkpoint()  # pausing here simply holds the work until resumed
        if info.duration:
            hooks.progress(audio_path, min(first_share, seg.end / info.duration * first_share))
        text = clean(seg)
        if text and not is_caption(text) and seg.end - seg.start >= 0.2:
            found.append((seg.start, seg.end, text))
        # Show a progress percentage every ~15 seconds for long files.
        if info.duration and time.time() - last_report > 15:
            log.info("   ... %d%%", min(first_share, seg.end / info.duration * first_share))
            last_report = time.time()

    # Second pass: Whisper reads 30-second windows and sometimes jumps over
    # a sung or chanted stretch to the next spoken line. Stretches of sound
    # (not dead air) with no text are transcribed again on their own, which
    # usually recovers them. Only confident lines are kept, so instrumental
    # music doesn't turn into invented text.
    if refill and clips:
        sound = clips if isinstance(clips, list) else [0.0, len(source) / 16000]
        holes = find_holes(found, sound)
        if holes:
            added = 0
            # No random retries here: on a short stretch on its own, the
            # plain best guess is the most reliable.
            second = dict(options, temperature=0.0)
            # The language is known by now: don't detect it again each time.
            if not second.get("language") and info is not None:
                second["language"] = info.language
            second.pop("language_detection_segments", None)

            def read_stretch(a, b):
                got = []
                # Only this stretch is handed over (not the whole recording),
                # so each second look takes seconds, not a full pass.
                piece = source[int(a * 16000):int(b * 16000)]
                if len(piece) < 16000:
                    return got
                extra, _ = model.transcribe(piece, **second)
                for seg in extra:
                    seg = dataclasses.replace(seg, start=seg.start + a, end=seg.end + a)
                    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                        continue  # probably invented over music
                    text = clean(seg)
                    if (text and not is_filler(text) and not is_caption(text)
                            and seg.end - seg.start > 0.3):
                        got.append((seg.start, seg.end, text))
                return got

            for n, (a, b) in enumerate(holes):
                hooks.checkpoint()
                hooks.progress(audio_path, first_share + (100 - first_share) * n / len(holes))
                got = read_stretch(a, b)
                if not got and b - a >= 2 * HOLE_MIN_SECONDS:
                    # Still nothing: try each half on its own once.
                    mid = (a + b) / 2
                    got = read_stretch(a, mid) + read_stretch(mid, b)
                found.extend(got)
                added += len(got)
            log.info("   Second pass over %d skipped stretch(es): %d line(s) recovered",
                     len(holes), added)
            found.sort(key=lambda f: f[0])

    # Tidy-up: invented "Thank you." lines and lines stuck repeating.
    found.sort(key=lambda f: f[0])
    found, dropped = drop_invented_fillers(found, music)
    if dropped:
        log.info("   Removed %d invented filler line(s) (e.g. \"%s\" at %s)", len(dropped),
                 dropped[0][2], format_timestamp(dropped[0][0]))
    found, merged = merge_repeated_lines(found)
    if merged:
        log.info("   Joined %d repeated line(s)", merged)

    # --- one continuous timeline ------------------------------------------
    # Every line starts where the one before ended. Pauses of 3 seconds or
    # more get their own line ([SILENCE ...], (music, no clear words), ...); shorter
    # ones are joined onto the line before, so the times never jump.
    entries = []  # (start, end, kind, text)
    for a, b, text in found:
        entries.append((a, b, "text", text))
    for (a, b), (label, saved) in jingle_hits.items():
        entries.append((a, b, label, " ".join(saved.split())))
    for a, b in gaps:
        entries.append((a, b, "SILENCE", ""))
    entries.sort(key=lambda e: (e[0], e[1]))

    have_audio = np is not None and isinstance(source, np.ndarray)
    total = len(source) / 16000 if have_audio else (info.duration if info else 0)

    def describe(a, b):
        """What fills a pause with no line: silence, music or a voice."""
        if have_audio:
            clip = source[int(a * 16000):int(b * 16000)]
            if len(clip) and 20 * np.log10(np.sqrt(np.mean(clip * clip)) + 1e-10) < -50:
                return "SILENCE", ""
        if music is not None:
            label = music_detect.describe_empty(music, a, b)
            return ("MUSIC" if label == "(music, no clear words)" else "text"), label
        return "text", "(no clear words)"

    timeline = []
    pos = 0.0
    for a, b, kind, text in entries:
        a = max(a, pos)
        if b <= a:
            continue
        if a - pos >= GAP_LABEL_SECONDS:
            k, t = describe(pos, a)
            timeline.append([pos, a, k, t])
        elif timeline:
            timeline[-1][1] = a          # tiny pause: close it up
        else:
            a = 0.0                      # first line starts at 0:00
        timeline.append([a, b, kind, text])
        pos = b
    if total and total - pos >= GAP_LABEL_SECONDS:
        k, t = describe(pos, total)
        timeline.append([pos, total, k, t])
    elif timeline and total:
        timeline[-1][1] = max(timeline[-1][1], total)

    # Neighbouring silences become one.
    merged = []
    for item in timeline:
        if merged and item[2] == merged[-1][2] == "SILENCE":
            merged[-1][1] = item[1]
        else:
            merged.append(item)

    for a, b, kind, text in merged:
        if kind == "text" and music is not None and music_detect.is_music(music, a, b):
            kind = "MUSIC"
        label = "" if kind == "text" else kind
        if config["include_timestamps"]:
            stamp = f"{format_timestamp(a)} → {format_timestamp(b)}"
            head = f"[{label} {stamp}]" if label else f"[{stamp}]"
            lines.append(f"{head} {text}".rstrip())
        elif label:
            lines.append(f"[{label}] {text}".rstrip())
        else:
            lines.append(text)

    # Unique temp name per PC and process, so two PCs can never write into
    # the same temp file on a shared folder.
    tmp_path = txt_path.with_name(f"{txt_path.name}.{HOSTNAME}-{os.getpid()}.part")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
            f.write("\n")
        os.replace(tmp_path, txt_path)
        set_created_time(txt_path, time.time())  # = when it was finished
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

def list_audio_entries(input_dir: Path, file_types) -> dict:
    """Audio/video files directly in input_dir (no subfolders), from ONE
    folder listing: name -> (path, size, modified, created).

    On a network folder, asking about each file separately costs a request
    per file (thousands for a big archive); the listing already carries
    all of this."""
    out = {}
    if not input_dir.is_dir():
        return out
    with os.scandir(input_dir) as entries:
        for e in entries:
            if Path(e.name).suffix.lower() not in file_types:
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()  # from the listing itself on Windows (no extra request)
            except OSError:
                continue
            out[e.name] = (Path(e.path), st.st_size, st.st_mtime,
                           getattr(st, "st_birthtime", st.st_ctime))
    return out


def list_audio_files(input_dir: Path, file_types: set) -> list:
    """Audio/video files directly in input_dir (no subfolders), sorted by name."""
    return sorted((v[0] for v in list_audio_entries(input_dir, file_types).values()),
                  key=lambda p: p.name.lower())


def folder_listing(folder: Path) -> dict:
    """Every file in a folder from one listing: lowercase name -> modified time."""
    out = {}
    if not folder.is_dir():
        return out
    with os.scandir(folder) as entries:
        for e in entries:
            try:
                if e.is_file():
                    out[e.name.lower()] = e.stat().st_mtime
            except OSError:
                pass
    return out


def arrival_time(path: Path, listed=None) -> float:
    """When the file appeared in the folder.

    Uses the later of "created" and "modified": a copied file keeps its old
    modified date but gets a new created date, while a file that is still
    being recorded keeps getting a newer modified date. `listed` (from
    list_audio_entries) saves asking the folder again.
    """
    hit = listed.get(path.name) if listed else None
    if hit:
        return max(hit[2], hit[3])
    try:
        st = path.stat()
    except OSError:
        return 0.0
    created = getattr(st, "st_birthtime", st.st_ctime)  # creation time on Windows
    return max(created, st.st_mtime)


def sort_queue(files: list, order: str, listed=None) -> list:
    """Order files for processing according to the "order" setting."""
    if order == "oldest_first":
        return sorted(files, key=lambda p: arrival_time(p, listed))
    if order == "name":
        return sorted(files, key=lambda p: p.name.lower())
    return sorted(files, key=lambda p: arrival_time(p, listed), reverse=True)  # newest_first


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
        self.unsaved = False
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

    def record(self, txt: Path, audio: Path, save: bool = True) -> None:
        """Record that txt was made from audio. save=False only notes it;
        save() then writes everything noted in one go (indexing a big
        archive once instead of rewriting the file per transcript)."""
        if self.owners.get(txt.name.lower()) == audio.name:
            return
        self.owners[txt.name.lower()] = audio.name
        self.unsaved = True
        if save:
            self.save()

    def save(self) -> None:
        if not self.unsaved:
            return
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
            self.unsaved = False
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
            if not self.is_ours():
                return  # another PC took it over (e.g. after a long network outage)
            try:
                os.utime(self.path)
            except OSError:
                pass

    def is_ours(self) -> bool:
        return self.owner_of(self.path) == (HOSTNAME, os.getpid())

    def release(self) -> None:
        if self._stop:
            self._stop.set()
        # Only remove the claim if it's still ours: if another PC took it over
        # while our network was down, deleting it would let a third PC start
        # the same file.
        if not self.is_ours():
            return
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


SETTLE_SECONDS = 60  # a file must stay unchanged this long before it's started


def created_time(path) -> float:
    """When a file was created. For transcripts this is when it was
    transcribed (edits keep it; see replace_keeping_created)."""
    st = os.stat(path)
    return getattr(st, "st_birthtime", st.st_ctime)


def set_created_time(path, when: float) -> None:
    """Set a file's 'created' date (Windows; works on shared folders too)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        # FILE_WRITE_ATTRIBUTES, share read/write/delete, OPEN_EXISTING
        handle = k32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x02000000, None)
        if not handle or handle == wintypes.HANDLE(-1).value:
            return
        try:
            ticks = int((when + 11644473600) * 10_000_000)
            created = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
            k32.SetFileTime(handle, ctypes.byref(created), None, None)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass


def replace_keeping_created(tmp, path) -> None:
    """os.replace(tmp, path), keeping path's 'created' date: an edited
    transcript still counts as transcribed when it really was (not as new)."""
    try:
        created = created_time(path)
    except OSError:
        created = None
    os.replace(tmp, path)
    if created is not None:
        set_created_time(path, created)


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
        # Files not done yet: name -> ((size, modified time), when we first saw
        # that). A file is only started once both have stayed the same for
        # SETTLE_SECONDS of this PC's clock, so files still being recorded or
        # copied (loggers write in bursts; network folders can report an old
        # size for a while) are left alone.
        self.pending_sizes = {}
        # Other PCs' claims we've seen: claim path -> (modified time, when we
        # first saw that modified time). Used to spot abandoned claims.
        self.claims_seen = {}
        self.started_scanning = False
        # The last Inbox listing (see list_audio_entries) and, during a scan,
        # the Transcripts folder listing: lowercase name -> modified time.
        self.listed = {}
        self._outputs = None

    def is_claimed_by_other_pc(self, txt: Path) -> bool:
        """True if another PC is working on this transcript right now."""
        claim = txt.with_name(txt.name + WorkClaim.SUFFIX)
        if self._outputs is not None:
            mtime = self._outputs.get(claim.name.lower())
        else:
            try:
                mtime = claim.stat().st_mtime
            except OSError:
                mtime = None
        if mtime is None:
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
        """Clear an abandoned claim. It's renamed first: if two PCs both
        decide it's abandoned, only one rename succeeds, so neither can delete
        the fresh claim the other has just made."""
        self.claims_seen.pop(claim, None)
        grave = claim.with_name(f"{claim.name}.stale-{HOSTNAME}-{os.getpid()}")
        try:
            os.rename(claim, grave)
        except OSError:
            return
        try:
            grave.unlink()
        except OSError:
            pass

    def needs_work(self, audio: Path) -> bool:
        """True if this file has no .txt yet, isn't being done by another
        PC, and hasn't already failed here."""
        txt = self.names.path_for(audio)
        listing = self._outputs
        if (txt.name.lower() in listing) if listing is not None else txt.exists():
            # Transcripts made before the index existed: record who owns them,
            # so a later file with the same name gets its own .txt. (Saved
            # once at the end of the scan.)
            self.names.record(txt, audio, save=listing is None)
            return False
        if audio.name in self.failed and self.failed[audio.name] == self.signature(audio):
            return False
        if self.is_claimed_by_other_pc(txt):
            return False
        return True

    RECENT_SECONDS = 6 * 3600  # files created this recently are checked one by one

    def signature(self, audio: Path, exact: bool = False):
        """(size, modified time) from the last listing, or asked directly.

        A folder listing can show an old size for a file that's still open
        and being written (Windows updates it lazily). So recently created
        files (the only ones that can still be recording or copying) are
        always asked directly; older ones come from the listing."""
        hit = self.listed.get(audio.name)
        if hit is None or exact or time.time() - hit[3] < self.RECENT_SECONDS:
            return file_signature(audio)
        return (hit[1], hit[2])

    def scan(self):
        """Look at the input folder once.

        Each folder is read with one listing (not one request per file), so
        this stays quick with thousands of recordings on a network share.

        Returns (ready, waiting, saw_new_file): files ready to transcribe in
        queue order, all files still to do, and whether a file was seen for
        the first time (so we re-check it soon).
        """
        self.listed = list_audio_entries(self.input_dir, self.config["file_types"])
        all_files = sorted((v[0] for v in self.listed.values()), key=lambda p: p.name.lower())
        self._outputs = folder_listing(self.output_dir)
        try:
            waiting = [p for p in all_files if self.needs_work(p)]
        finally:
            self._outputs = None
        self.names.save()  # transcripts newly found without an index entry, in one go
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
        now = time.time()
        settle = float(self.config.get("settle_seconds", SETTLE_SECONDS))
        for p in waiting:
            sig = self.signature(p)
            previous = self.pending_sizes.get(p.name)
            if previous is None or previous[0] != sig:
                self.pending_sizes[p.name] = (sig, now)
                if previous is None:
                    saw_new_file = True
                    if self.started_scanning:
                        log.info("New file: %s (starts once it has stopped growing)", p.name)
                continue
            if sig and sig[0] and now - previous[1] >= settle and can_open(p):
                ready.append(p)

        self.started_scanning = True
        return sort_queue(ready, self.config["order"], self.listed), waiting, saw_new_file

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
            before = file_signature(audio)
            transcribe_file(self.model, audio, txt, self.config, self.hooks)
            if file_signature(audio) != before:
                # Still being recorded or copied after all: throw this
                # transcript away; the file is redone once it settles.
                log.warning("%s changed while it was being transcribed (still being "
                            "recorded?). It will be redone once it stops changing.", audio.name)
                try:
                    txt.unlink()
                except OSError:
                    pass
                self.pending_sizes.pop(audio.name, None)
                return
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
                        files=sort_queue(waiting, self.config["order"], self.listed),
                        listed=self.listed)

            if saw_new_file and not ready:
                # Something new arrived: look again soon (it starts once it has
                # stopped changing), rather than waiting the full interval.
                hooks.sleep(min(interval, 10))
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
