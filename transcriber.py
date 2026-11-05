"""
MP3 Transcriber
===============

Watches a folder for audio files (.mp3, .mp4, .m4a, .wav, ...) and transcribes each one to a .txt file
using faster-whisper.

How it works:
  1. Reads settings from config.json (created with defaults if missing).
  2. Loads the Whisper model once (GPU if available, otherwise CPU).
  3. Transcribes every audio file in the input folder that has no .txt yet.
  4. Keeps watching the input folder and transcribes new files as they arrive.

The original audio files are only ever READ. They are never changed, moved,
or deleted.
"""

import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path


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
    # Defaults: Documents\mp3 and Documents\transcribed of whoever runs it.
    "input_folder": str(DOCUMENTS_DIR / "mp3"),
    "output_folder": str(DOCUMENTS_DIR / "transcribed"),
    # tiny, base, small, medium, large-v3
    "model_size": "small",
    # "auto" = detect the language. Or force one, e.g. "en" or "tl" (Tagalog).
    "language": "auto",
    "include_timestamps": False,
    # Skip silence. Good for speech recordings, but it also throws away
    # singing over music - set to false for songs.
    "vad_filter": True,
    # Give the model the cleaned-up file name (e.g. "Olivia Rodrigo - drop
    # dead") as a hint. Helps it spell names and song titles correctly.
    "use_filename_hint": True,
    # Which files to pick up. Audio is pulled out of video files automatically.
    "file_types": [".mp3", ".mp4", ".m4a", ".wav", ".flac", ".ogg", ".opus",
                   ".webm", ".aac", ".wma", ".mkv", ".mov"],
    "poll_interval_seconds": 10,
    # "auto" = GPU if available, else CPU. Or force "cuda" or "cpu".
    "device": "auto",
}

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3"}


def load_config() -> dict:
    """Load config.json, creating it with defaults if it doesn't exist.

    Missing keys are filled in from DEFAULT_CONFIG, so an old config file
    keeps working after new settings are added.
    """
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        print(f"Created default config file: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        try:
            user_config = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"config.json is not valid JSON ({e}).\n"
                "Tip: backslashes in Windows paths must be doubled, "
                r'e.g. "C:\\Users\\YourName\\Documents\\mp3"'
            ) from None

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

    config["device"] = str(config.get("device") or "auto").strip().lower()
    config["include_timestamps"] = bool(config["include_timestamps"])
    config["vad_filter"] = bool(config["vad_filter"])
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

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.addFilter(only_ours)
    root.addHandler(console)

    logfile = logging.FileHandler(LOG_PATH, encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logfile.addFilter(only_ours)
    root.addHandler(logfile)


log = logging.getLogger("transcriber")


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
        # Skip codes like "ytmp4" or "1080p" (letters mixed with digits).
        if any(c.isdigit() for c in word) and not word.isdigit():
            continue
        words.append(word)
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


def transcribe_file(model, audio_path: Path, txt_path: Path, config: dict) -> None:
    """Transcribe one audio/video file and write the .txt file.

    The text is first written to a temporary ".part" file and renamed at the
    end. That way a half-finished transcript (e.g. if the program is closed)
    never looks like a finished one.
    """
    hint = filename_hint(audio_path) if config["use_filename_hint"] else None
    if hint:
        log.info('   Using hint from file name: "%s"', hint)

    segments, info = model.transcribe(
        str(audio_path),
        language=config["language"],
        vad_filter=config["vad_filter"],  # skip silence
        hotwords=hint,
        # Check several 30s chunks when detecting the language, not just the
        # first one (song intros are often instrumental).
        language_detection_segments=4,
    )

    if config["language"] is None:
        log.info("   Detected language: %s (%.0f%% sure), audio length %s",
                 info.language, info.language_probability * 100,
                 format_timestamp(info.duration))

    # The silence filter can mistake singing over music for "no speech".
    # Warn if it threw away most of the file.
    kept = getattr(info, "duration_after_vad", None)
    if config["vad_filter"] and kept is not None and info.duration > 30 and kept < info.duration * 0.5:
        log.warning("   Silence filter kept only %s of %s. If this is music, set "
                    '"vad_filter": false in config.json to transcribe everything.',
                    format_timestamp(kept), format_timestamp(info.duration))

    lines = []
    last_report = time.time()
    for seg in segments:  # segments are produced as the audio is processed
        text = seg.text.strip()
        if not text or is_hallucination(text):
            continue
        if config["include_timestamps"]:
            lines.append(f"[{format_timestamp(seg.start)} → {format_timestamp(seg.end)}] {text}")
        else:
            lines.append(text)

        # Show a progress percentage every ~15 seconds for long files.
        if info.duration and time.time() - last_report > 15:
            pct = min(100, seg.end / info.duration * 100)
            log.info("   ... %d%%", pct)
            last_report = time.time()

    tmp_path = txt_path.with_name(txt_path.name + ".part")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
        f.write("\n")
    os.replace(tmp_path, txt_path)


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


def output_path_for(audio_path: Path, output_dir: Path) -> Path:
    """meeting1.mp3 -> <output_dir>/meeting1.txt"""
    return output_dir / (audio_path.stem + ".txt")


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
    def __init__(self, model, config: dict):
        self.model = model
        self.config = config
        self.input_dir = Path(config["input_folder"])
        self.output_dir = Path(config["output_folder"])
        # Files that failed: name -> signature. Not retried unless the file changes.
        self.failed = {}
        # Last seen size of files waiting to finish copying: name -> size.
        self.pending_sizes = {}

    def needs_work(self, audio: Path) -> bool:
        """True if this file has no .txt yet and hasn't already failed."""
        if output_path_for(audio, self.output_dir).exists():
            return False
        if audio.name in self.failed and self.failed[audio.name] == file_signature(audio):
            return False
        return True

    def process_batch(self, files: list) -> None:
        """Transcribe files one at a time. One bad file never stops the rest."""
        total = len(files)
        for i, audio in enumerate(files, start=1):
            txt = output_path_for(audio, self.output_dir)
            log.info("[%d/%d] Transcribing %s...", i, total, audio.name)
            start = time.time()
            try:
                transcribe_file(self.model, audio, txt, self.config)
                log.info("Done → %s (took %.0fs)", txt.name, time.time() - start)
                self.failed.pop(audio.name, None)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("FAILED %s: %s (skipping it)", audio.name, e)
                # Full details go to the log file only.
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
                self.failed[audio.name] = file_signature(audio)
                # Clean up any leftover partial output.
                part = txt.with_name(txt.name + ".part")
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass

    def first_pass(self) -> None:
        """Transcribe everything already in the input folder."""
        all_files = list_audio_files(self.input_dir, self.config["file_types"])
        todo = [p for p in all_files if self.needs_work(p)]
        skipped = len(all_files) - len(todo)

        log.info("Found %d audio file%s", len(all_files), "" if len(all_files) == 1 else "s")
        if skipped:
            log.info("Skipping %d already transcribed", skipped)
        if not todo:
            return

        # Quick check that nothing is still being copied right now.
        sizes = {p.name: get_size(p) for p in todo}
        time.sleep(2)
        ready = []
        for p in todo:
            if get_size(p) == sizes[p.name] and sizes[p.name] and can_open(p):
                ready.append(p)
            else:
                log.info("%s is still being copied - will pick it up when it's done.", p.name)
        self.process_batch(ready)

    def watch_once(self) -> None:
        """One check of the input folder. A new file is only transcribed once
        its size has stayed the same for 2 checks in a row."""
        waiting = [p for p in list_audio_files(self.input_dir, self.config["file_types"]) if self.needs_work(p)]
        waiting_names = {p.name for p in waiting}

        # Forget files that disappeared or got done.
        for name in list(self.pending_sizes):
            if name not in waiting_names:
                del self.pending_sizes[name]

        ready = []
        for p in waiting:
            size = get_size(p)
            previous = self.pending_sizes.get(p.name)
            if size and size == previous and can_open(p):
                ready.append(p)
                del self.pending_sizes[p.name]
            else:
                if previous is None:
                    log.info("New file: %s (waiting for copy to finish)", p.name)
                self.pending_sizes[p.name] = size

        if ready:
            log.info("Found %d new audio file%s", len(ready), "" if len(ready) == 1 else "s")
            self.process_batch(ready)
            log.info("Watching %s for new audio files... (Ctrl+C to stop)", self.input_dir)

    def run_forever(self) -> None:
        self.first_pass()
        interval = self.config["poll_interval_seconds"]
        log.info("Watching %s for new audio files every %ds... (Ctrl+C to stop)",
                 self.input_dir, interval)
        while True:
            time.sleep(interval)
            try:
                self.watch_once()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # e.g. the folder is temporarily unavailable - just try again.
                log.error("Error while checking folder: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    log.info("=" * 60)
    log.info("MP3 Transcriber starting")

    config = load_config()
    input_dir = Path(config["input_folder"])
    output_dir = Path(config["output_folder"])

    log.info("Input folder : %s", input_dir)
    log.info("Output folder: %s", output_dir)
    log.info("Model: %s | Language: %s | Timestamps: %s | Silence filter: %s",
             config["model_size"], config["language"] or "auto-detect",
             "on" if config["include_timestamps"] else "off",
             "on" if config["vad_filter"] else "off")

    # Create both folders if needed, so first-time users get a folder ready
    # to drop files into.
    for folder in (input_dir, output_dir):
        if not folder.is_dir():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                log.info("Created folder: %s", folder)
            except OSError as e:
                raise OSError(
                    f"Could not create the folder {folder} ({e}).\n"
                    f"Edit input_folder / output_folder in {CONFIG_PATH}"
                ) from None

    model = load_model(config)
    Transcriber(model, config).run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception:
        # Show the error instead of letting the window vanish.
        print("\n" + "=" * 60)
        print("ERROR - the program stopped:")
        traceback.print_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("FATAL ERROR:\n" + traceback.format_exc())
        except OSError:
            pass
        print("=" * 60)
        try:
            input("Press Enter to exit...")
        except (EOFError, KeyboardInterrupt):
            pass
        sys.exit(1)
