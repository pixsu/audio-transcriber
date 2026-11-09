"""Find the music parts of a recording (songs, jingles, music beds).

Uses YAMNet, a small sound classifier from Google trained on AudioSet
(Qualcomm AI Hub's ONNX export, MIT licence). It runs on the onnxruntime
library that faster-whisper already ships with, fully offline.

The audio is cut into 0.96-second patches; each patch gets a score for
"Music", "Singing" and "Speech". Transcript lines are then labelled MUSIC
when music clearly dominates their time span. Nothing is removed: the
labels are only added in front of the lines.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("transcriber")

SAMPLE_RATE = 16000
PATCH_FRAMES = 96          # 96 x 10 ms = 0.96 s per patch
MEL_BANDS = 64
WINDOW, HOP, FFT = 400, 160, 512   # 25 ms window, 10 ms hop
PATCH_SECONDS = PATCH_FRAMES * HOP / SAMPLE_RATE

MODEL_FILE = "yamnet.onnx"
# Qualcomm AI Hub's official YAMNet export (MIT licence), downloaded once
# into the models folder. The fingerprint stops a changed or damaged file
# from being used.
MODEL_URL = ("https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/"
             "models/yamnet/releases/v0.66.0/yamnet-onnx-float.zip")
MODEL_SHA256 = "cf08212c3b9c03160d3c343a5cf99dd53edcb3176b49b6dd330d7d0480abd309"
MODEL_FOLDER = "yamnet"
MODEL_FILES = ("yamnet.onnx", "yamnet.data", "labels.txt")


def ensure_model(models_dir: Path) -> Path:
    """The folder with the music model, downloading it (about 14 MB) if needed."""
    import hashlib
    import io
    import urllib.request
    import zipfile
    folder = Path(models_dir) / MODEL_FOLDER
    if all((folder / f).exists() for f in MODEL_FILES):
        return folder
    log.info("Downloading the music detection model (about 14 MB)...")
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "AudioTranscriber"})
    with urllib.request.urlopen(request, timeout=60) as r:
        data = r.read(64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise RuntimeError("the downloaded music model didn't pass the safety check")
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if name in MODEL_FILES:
                (folder / name).write_bytes(z.read(info))
    return folder


def _mel_matrix():
    """64 mel bands from 125 to 7500 Hz (HTK scale), as YAMNet expects."""
    def hz_to_mel(f):
        return 1127.0 * np.log1p(np.asarray(f, dtype=np.float64) / 700.0)

    bins = FFT // 2 + 1
    bin_mel = hz_to_mel(np.linspace(0, SAMPLE_RATE / 2, bins))
    edges = np.linspace(hz_to_mel(125.0), hz_to_mel(7500.0), MEL_BANDS + 2)
    lower, center, upper = edges[:-2], edges[1:-1], edges[2:]
    up = (bin_mel[:, None] - lower) / (center - lower)
    down = (upper - bin_mel[:, None]) / (upper - center)
    weights = np.maximum(0.0, np.minimum(up, down))
    weights[0, :] = 0.0  # ignore the DC bin
    return weights.astype(np.float32)


def log_mel(audio: np.ndarray) -> np.ndarray:
    """(frames, 64) log-mel spectrogram, 10 ms per frame."""
    if len(audio) < WINDOW:
        audio = np.pad(audio, (0, WINDOW - len(audio)))
    count = 1 + (len(audio) - WINDOW) // HOP
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(WINDOW) / WINDOW)).astype(np.float32)
    mel = _mel_matrix()
    out = np.empty((count, MEL_BANDS), dtype=np.float32)
    step = 20000  # frames per block, keeps memory low on long files
    for start in range(0, count, step):
        n = min(step, count - start)
        idx = (start + np.arange(n))[:, None] * HOP + np.arange(WINDOW)
        frames = audio[idx] * window
        spectrum = np.abs(np.fft.rfft(frames, FFT)).astype(np.float32)
        out[start:start + n] = np.log(spectrum @ mel + 0.001)
    return out


class MusicDetector:
    def __init__(self, model_dir: Path):
        import onnxruntime as ort
        path = Path(model_dir) / MODEL_FILE
        if not path.exists():
            raise FileNotFoundError(f"Music detection model not found: {path}")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        labels = (Path(model_dir) / "labels.txt").read_text(encoding="utf-8").splitlines()
        self.music_idx = labels.index("Music")
        self.singing_idx = labels.index("Singing")
        self.speech_idx = labels.index("Speech")

    def scan(self, audio: np.ndarray, should_stop=None):
        """Scores per 0.96 s patch: array of (music, speech)."""
        mel = log_mel(audio)
        patches = len(mel) // PATCH_FRAMES
        scores = np.zeros((patches, 2), dtype=np.float32)
        for i in range(patches):
            if should_stop and i % 200 == 0:
                should_stop()
            patch = mel[i * PATCH_FRAMES:(i + 1) * PATCH_FRAMES][None, None]
            out = self.session.run(None, {self.input: patch})[0][0]
            out = 1.0 / (1.0 + np.exp(-np.clip(out, -30, 30)))  # raw scores -> 0..1
            scores[i] = (max(out[self.music_idx], out[self.singing_idx]), out[self.speech_idx])
        return scores


# "Dead air": stretches with no sound at all. Quieter than -50 dBFS for at
# least 10 seconds. Quiet music and soft singing are far louder than this.
SILENCE_DBFS = -50.0
SILENCE_MIN_SECONDS = 10.0
SILENCE_STEP = 0.5   # seconds per loudness measurement
SILENCE_MARGIN = 0.5  # seconds of the gap kept at each edge


def find_silence(audio: np.ndarray):
    """[(start, end), ...] in seconds for every long stretch of dead air."""
    step = int(SILENCE_STEP * SAMPLE_RATE)
    count = len(audio) // step
    if not count:
        return []
    blocks = audio[:count * step].reshape(count, step).astype(np.float32)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1)) + 1e-10
    quiet = 20 * np.log10(rms) < SILENCE_DBFS
    gaps, start = [], None
    for i, q in enumerate(list(quiet) + [False]):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * SILENCE_STEP >= SILENCE_MIN_SECONDS:
                a = start * SILENCE_STEP + (SILENCE_MARGIN if start else 0)
                # A gap that runs to the end of the file ends at the very end.
                end = i * SILENCE_STEP if i < count else len(audio) / SAMPLE_RATE
                b = end - (SILENCE_MARGIN if i < count else 0)
                gaps.append((a, b))
            start = None
    return gaps


def sound_clips(gaps, duration: float):
    """The parts between the gaps, as faster-whisper's clip_timestamps."""
    clips, pos = [], 0.0
    for a, b in gaps + [(duration, duration)]:
        if a - pos >= 1.0:  # Whisper invents text for tiny slivers of audio
            clips += [pos, a]
        pos = b
    return clips


def is_music(scores, start: float, end: float) -> bool:
    """True when music clearly dominates the time span (seconds).

    Talking over a music bed (a DJ over an intro, an ad with background
    music) usually scores high for speech too, so it stays unlabelled."""
    if scores is None or not len(scores):
        return False
    a = max(0, int(start / PATCH_SECONDS))
    b = min(len(scores), max(a + 1, int(np.ceil(end / PATCH_SECONDS))))
    if a >= len(scores):
        return False
    music = float(np.mean(scores[a:b, 0]))
    speech = float(np.mean(scores[a:b, 1]))
    return music >= MUSIC_MIN and music >= speech + MUSIC_MARGIN


# Tuned on Filipino FM radio recordings (songs, jingles, DJ talk, ads).
MUSIC_MIN = 0.5
MUSIC_MARGIN = 0.15
