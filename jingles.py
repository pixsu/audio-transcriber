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
MATCH_SCORE = 0.93        # same broadcast: ~0.99; anything else: below 0.90
PIECE = 80                # matched in 8-second pieces (0.1 s columns)
MIN_SECONDS = 3.0
MAX_SECONDS = 600.0       # songs up to 10 minutes
KINDS = {"jingle": "JINGLE", "ad": "AD", "song": "SONG"}
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
                start: float, end: float, kind: str = "jingle", lines=None) -> Path:
    """Store the stretch start..end (seconds) of decoded 16 kHz audio.

    lines: optional [(start, end, text), ...] in seconds from the start of
    the stretch, for line-by-line text (song lyrics); otherwise `text` is
    written as one line."""
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
            "kind": kind if kind in KINDS else "jingle",
            "created": time.strftime("%Y-%m-%d %H:%M")}
    if lines:
        meta["lines"] = [[round(a, 2), round(b, 2), t.strip()] for a, b, t in lines if t.strip()]
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

def _pieces(length: int):
    """Start columns of the 8-second pieces of a fingerprint (the last one
    lines up with the end, so the whole thing is covered)."""
    if length <= PIECE:
        return [0]
    starts = list(range(0, length - PIECE + 1, PIECE))
    if starts[-1] != length - PIECE:
        starts.append(length - PIECE)
    return starts


def find_jingles(audio: np.ndarray, library: list, should_stop=None) -> list:
    """Every place a saved jingle / ad / song plays in the audio.

    Each saved item is compared in 8-second pieces, so a song that's cut
    short, faded early or talked over still matches on the pieces that are
    clean. Returns [(start, end, item, offset), ...] in seconds, sorted and
    never overlapping; offset is where in the saved item the match begins."""
    if not library:
        return []
    file_fp = fingerprint(audio)
    n = len(file_fp)
    found = []
    if n:
        size = 1 << int(np.ceil(np.log2(n + PIECE)))
        file_f = np.fft.rfft(file_fp, size, axis=0)  # once per file
    for item in library:
        if should_stop:
            should_stop()
        fp = item["fp"]
        length = len(fp)
        if not n or length == 0 or n < min(length, PIECE):
            continue
        piece_len = min(PIECE, length)
        votes = []  # (alignment, piece start)
        for k in _pieces(length):
            piece = fp[k:k + piece_len]
            scores = np.fft.irfft((file_f * np.conj(np.fft.rfft(piece, size, axis=0))).sum(axis=1),
                                  size)[:n - piece_len + 1] / piece_len
            hits = np.flatnonzero(scores >= MATCH_SCORE)
            last = -10 ** 9
            for i in hits:  # strongest position in each cluster of hits
                if i - last < piece_len // 2:
                    continue
                window = scores[i:i + piece_len // 2]
                j = i + int(np.argmax(window))
                votes.append((j - k, k))
                last = j
        if not votes:
            continue
        pieces_total = len(_pieces(length))
        need = 1 if pieces_total == 1 else max(2, int(np.ceil(pieces_total * 0.3)))
        votes.sort()
        group = [votes[0]]
        for v in votes[1:] + [(10 ** 9, 0)]:
            if v[0] - group[-1][0] <= 3:
                group.append(v)
                continue
            ks = sorted({k for _, k in group})
            if len(ks) >= need:
                align = int(np.median([a for a, _ in group]))
                first, last_k = ks[0], ks[-1] + piece_len
                if (last_k - first) >= 0.8 * length:  # nearly all of it: use it all
                    first, last_k = 0, length
                start = (align + first) * FRAME_SECONDS
                end = (align + last_k) * FRAME_SECONDS
                found.append((len(ks), start, end, item, first * FRAME_SECONDS))
            group = [v]
    # Most pieces first; drop anything overlapping a better match.
    chosen = []
    for _, a, b, item, offset in sorted(found, key=lambda f: -f[0]):
        if all(b <= ca or a >= cb for ca, cb, *_ in chosen):
            chosen.append((max(0.0, a), b, item, offset))
    return sorted(chosen, key=lambda c: c[0])


def lines_for_match(item: dict, start: float, end: float, offset: float):
    """The text lines for one match: [(start, end, text), ...] in file time."""
    label = KINDS.get(item.get("kind"), "JINGLE")
    lines = item.get("lines")
    if not lines:
        return [(start, end, item.get("text") or item.get("name", ""), label)]
    out = []
    span = end - start
    for a, b, text in lines:
        mid = (a + b) / 2 - offset
        if 0 <= mid <= span:
            out.append((start + a - offset, start + b - offset, text, label))
    return out or [(start, end, item.get("text") or item.get("name", ""), label)]
