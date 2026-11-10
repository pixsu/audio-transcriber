"""Jingle library: recognise audio that airs again and again.

Station jingles, IDs and ads are the exact same recording every time they
play. You correct one once ("Save as jingle" in the transcript window); its
sound fingerprint and your text are stored. When the same audio turns up in
another file it is recognised and your text is written instead of Whisper's
guess, and that stretch isn't transcribed at all.

The fingerprint is a coarse frequency picture of the sound (64 bands, one
column every 0.1 s). Matching slides it along the recording and compares
shapes, so loudness differences don't matter.

The library lives in a hidden ".jingles" folder inside the Transcripts
folder, so every PC that shares that folder shares the library.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import numpy as np

import music_detect

log = logging.getLogger("transcriber")

SAMPLE_RATE = 16000
STEP = 10                 # 10 x 10 ms = one fingerprint column per 0.1 s
FRAME_SECONDS = STEP * music_detect.HOP / SAMPLE_RATE
MATCH_SCORE = 0.93        # same broadcast: ~0.99; anything else: below 0.85
MIN_SECONDS = 3.0
MAX_SECONDS = 180.0
FOLDER_NAME = ".jingles"


def library_folder(output_folder) -> Path:
    return Path(output_folder) / FOLDER_NAME


def fingerprint(audio: np.ndarray) -> np.ndarray:
    """(columns, 64) float32, each column with zero mean and unit length."""
    mel = music_detect.log_mel(audio)
    n = len(mel) // STEP
    if not n:
        return np.zeros((0, music_detect.MEL_BANDS), dtype=np.float32)
    f = mel[:n * STEP].reshape(n, STEP, -1).mean(axis=1)
    f = f - f.mean(axis=1, keepdims=True)
    f /= np.linalg.norm(f, axis=1, keepdims=True) + 1e-6
    return f.astype(np.float32)


def _scores(file_fp: np.ndarray, jingle_fp: np.ndarray) -> np.ndarray:
    """Similarity (about -1..1) of the jingle at every position in the file."""
    n, length = len(file_fp), len(jingle_fp)
    if length == 0 or n < length:
        return np.zeros(0)
    size = 1 << int(np.ceil(np.log2(n + length)))
    acc = np.zeros(size, dtype=np.float64)
    for band in range(file_fp.shape[1]):
        acc += np.fft.irfft(np.fft.rfft(file_fp[:, band], size)
                            * np.conj(np.fft.rfft(jingle_fp[:, band], size)), size)
    return acc[:n - length + 1] / length


# --------------------------------------------------------------- library

def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\- ]+", "", name).strip()[:60] or "jingle"


def save_jingle(output_folder, name: str, text: str, audio: np.ndarray,
                start: float, end: float) -> Path:
    """Store the stretch start..end (seconds) of decoded 16 kHz audio."""
    if end - start < MIN_SECONDS:
        raise ValueError(f"A jingle must be at least {MIN_SECONDS:.0f} seconds long.")
    if end - start > MAX_SECONDS:
        raise ValueError(f"A jingle can be at most {MAX_SECONDS / 60:.0f} minutes long.")
    clip = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
    fp = fingerprint(clip)
    if len(fp) < MIN_SECONDS / FRAME_SECONDS:
        raise ValueError("That part of the recording is too short.")
    folder = library_folder(output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{_safe_name(name)}-{int(time.time())}"
    np.save(folder / f"{base}.npy", fp)
    meta = {"name": name.strip(), "text": text.strip(), "seconds": round(end - start, 2),
            "created": time.strftime("%Y-%m-%d %H:%M")}
    tmp = folder / f"{base}.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, folder / f"{base}.json")
    _cache.clear()
    return folder / f"{base}.json"


_cache: dict = {}


def load_library(output_folder) -> list:
    """[{"id", "name", "text", "seconds", "fp"}, ...] (cached until it changes)."""
    folder = library_folder(output_folder)
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return []
    key = (str(folder), tuple((f.name, f.stat().st_mtime) for f in files))
    if _cache.get("key") == key:
        return _cache["items"]
    items = []
    for meta_path in files:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fp = np.load(meta_path.with_suffix(".npy"))
            items.append(dict(meta, id=meta_path.stem, fp=fp))
        except (OSError, ValueError) as e:
            log.warning("Skipping damaged jingle %s (%s)", meta_path.name, e)
    _cache.update(key=key, items=items)
    return items


def delete_jingle(output_folder, jingle_id: str) -> None:
    folder = library_folder(output_folder)
    for ext in (".json", ".npy"):
        try:
            (folder / f"{jingle_id}{ext}").unlink()
        except FileNotFoundError:
            pass
    _cache.clear()


# --------------------------------------------------------------- matching

def find_jingles(audio: np.ndarray, library: list, should_stop=None) -> list:
    """[(start, end, jingle), ...] in seconds, sorted, never overlapping."""
    if not library:
        return []
    file_fp = fingerprint(audio)
    hits = []
    for jingle in library:
        if should_stop:
            should_stop()
        scores = _scores(file_fp, jingle["fp"])
        length = len(jingle["fp"])
        if not len(scores):
            continue
        # Take the best position, blank out around it, repeat.
        scores = scores.copy()
        while True:
            i = int(np.argmax(scores))
            if scores[i] < MATCH_SCORE:
                break
            hits.append((float(scores[i]), i * FRAME_SECONDS,
                         (i + length) * FRAME_SECONDS, jingle))
            scores[max(0, i - length):i + length] = -1
    # Strongest first; drop anything overlapping a stronger match.
    chosen = []
    for score, a, b, jingle in sorted(hits, key=lambda h: -h[0]):
        if all(b <= ca or a >= cb for ca, cb, _ in chosen):
            chosen.append((a, b, jingle))
    return sorted(chosen, key=lambda c: c[0])
