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
PREVIEW_SECONDS = 30      # stored with each item, to play from the list


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
    # A short preview (first 30 s) so it can be played from the list later.
    try:
        import wave
        preview = clip[:PREVIEW_SECONDS * SAMPLE_RATE]
        with wave.open(str(folder / f"{base}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(preview, -1, 1) * 32767).astype("<i2").tobytes())
    except Exception as e:
        log.warning("Couldn't save the preview: %s", e)
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
            wav = meta_path.with_suffix(".wav")
            items.append(dict(meta, id=meta_path.stem, fp=fp,
                              preview=str(wav) if wav.exists() else None))
        except (OSError, ValueError) as e:
            log.warning("Skipping damaged jingle %s (%s)", meta_path.name, e)
    _cache.update(key=key, items=items)
    return items


def delete_jingle(output_folder, jingle_id: str) -> None:
    folder = library_folder(output_folder)
    for ext in (".json", ".npy", ".wav"):
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


# --------------------------------------------------------------- repeats

def _codes(audio: np.ndarray):
    """One 32-bit sound code every 10 ms (how the loudness of neighbouring
    frequency bands changes), plus loudness, for spotting repeated audio."""
    mel = music_detect.log_mel(audio)
    k = 10  # smoothed over 0.1 s, so small timing differences don't matter
    c = np.cumsum(np.vstack([np.zeros((1, mel.shape[1]), np.float32), mel]), axis=0)
    sm = (c[k:] - c[:-k]) / k
    edges = np.linspace(0, mel.shape[1], 34).astype(int)
    bands = np.stack([sm[:, edges[i]:edges[i + 1]].mean(axis=1) for i in range(33)], axis=1)
    d = bands[:, :-1] - bands[:, 1:]
    lag = 5
    bits = (d[lag:] - d[:-lag]) > 0
    codes = (bits.astype(np.uint64) << np.arange(32, dtype=np.uint64)).sum(axis=1)
    return codes.astype(np.uint32), sm[lag:].mean(axis=1)


CODE_STEP = 0.01          # seconds per code
REPEAT_MIN_VOTES = 25     # matching codes needed for a stretch to count


def find_repeats(audio_paths, library=(), progress=None, should_stop=None,
                 min_seconds=4.0, max_seconds=420.0):
    """Stretches of audio that air more than once across these recordings
    (and aren't in the library yet).

    Returns [{"seconds", "count", "places": [(path, start, end), ...]}, ...],
    most useful first (aired most often, then longest)."""
    from collections import defaultdict
    from faster_whisper.audio import decode_audio

    all_codes, all_file, all_pos, known = [], [], [], defaultdict(list)
    paths = list(audio_paths)
    for fi, path in enumerate(paths):
        if should_stop:
            should_stop()
        if progress:
            progress(fi / max(1, len(paths)) * 0.8, f"Reading {Path(path).name}")
        try:
            audio = decode_audio(str(path))
        except Exception as e:
            log.warning("Repeat finder skipped %s (%s)", Path(path).name, e)
            continue
        codes, loud = _codes(audio)
        keep = np.flatnonzero(loud > -8)  # not dead air
        all_codes.append(codes[keep])
        all_file.append(np.full(len(keep), fi, np.int32))
        all_pos.append(keep.astype(np.int32))
        for a, b, _item, _off in find_jingles(audio, list(library)):
            known[fi].append((a, b))
    if not all_codes:
        return []
    if progress:
        progress(0.85, "Comparing recordings")
    codes = np.concatenate(all_codes)
    fid = np.concatenate(all_file)
    pos = np.concatenate(all_pos)
    order = np.argsort(codes, kind="stable")
    codes, fid, pos = codes[order], fid[order], pos[order]
    cuts = np.flatnonzero(np.diff(codes)) + 1
    starts = np.concatenate([[0], cuts])
    ends = np.concatenate([cuts, [len(codes)]])
    sizes = ends - starts
    # The same code in different places, lined up by time offset.
    hits = defaultdict(list)  # (file A, file B, offset in 0.1 s) -> positions in A
    for s, e in zip(starts[(sizes >= 2) & (sizes <= 30)], ends[(sizes >= 2) & (sizes <= 30)]):
        f, q = fid[s:e], pos[s:e]
        for i in range(e - s):
            for j in range(i + 1, e - s):
                a, b = (i, j) if (f[i], q[i]) < (f[j], q[j]) else (j, i)
                if f[a] == f[b] and q[b] - q[a] < 12000:
                    continue  # same file, under 2 minutes apart: a song's own chorus
                hits[(int(f[a]), int(f[b]), int(q[b] - q[a]) // 10)].append(int(q[a]))
    # Each well-supported offset -> matching stretches in both recordings.
    places = []      # (file, start, end) in seconds
    links = []       # pairs of indexes into places that are the same audio
    for (fa, fb, off), qs in hits.items():
        if len(qs) < REPEAT_MIN_VOTES:
            continue
        qs = np.sort(np.asarray(qs))
        groups = np.split(qs, np.flatnonzero(np.diff(qs) > 300) + 1)  # break at 3 s gaps
        for g in groups:
            if len(g) < REPEAT_MIN_VOTES:
                continue
            a0, a1 = g[0] * CODE_STEP, (g[-1] + 15) * CODE_STEP
            if not min_seconds <= a1 - a0 <= max_seconds:
                continue
            shift = off * 10 * CODE_STEP
            places.append((fa, a0, a1))
            places.append((fb, a0 + shift, a1 + shift))
            links.append((len(places) - 2, len(places) - 1))
    if not places:
        return []
    # Join places that overlap in the same recording (same clip found via
    # several pairs of recordings).
    parent = list(range(len(places)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in links:
        parent[root(a)] = root(b)
    by_file = defaultdict(list)
    for i, (f, a, b) in enumerate(places):
        by_file[f].append((a, b, i))
    for spans in by_file.values():
        spans.sort()
        for (a1, b1, i1), (a2, b2, i2) in zip(spans, spans[1:]):
            if a2 < b1 - 0.5 * min(b1 - a1, b2 - a2):
                parent[root(i1)] = root(i2)
    clusters = defaultdict(list)
    for i in range(len(places)):
        clusters[root(i)].append(places[i])
    results = []
    for members in clusters.values():
        # One place per airing: merge overlapping places in the same file.
        airings = []
        for f, a, b in sorted(members):
            if airings and airings[-1][0] == f and a < airings[-1][2] + 60:  # one airing
                airings[-1] = (f, airings[-1][1], max(b, airings[-1][2]))
            else:
                airings.append((f, a, b))
        if len(airings) < 2:
            continue
        if any(any(a < kb and b > ka and min(b, kb) - max(a, ka) > 0.5 * (b - a)
                   for ka, kb in known.get(f, [])) for f, a, b in airings):
            continue  # already in the library
        seconds = float(np.median([b - a for _, a, b in airings]))
        results.append({"seconds": round(seconds, 1), "count": len(airings),
                        "places": [(paths[f], round(float(a), 1), round(float(b), 1))
                                   for f, a, b in airings]})
    results.sort(key=lambda r: (-r["count"], -r["seconds"]))
    if progress:
        progress(1.0, "Done")
    return results


# --------------------------------------------------------------- exact edges

def decode_window(path, start: float, end: float) -> np.ndarray:
    """Just start..end seconds of a recording, as 16 kHz mono (much faster
    than decoding a whole hour)."""
    import av
    start = max(0.0, float(start))
    out, need = [], int((end - start) * SAMPLE_RATE)
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        try:
            container.seek(int(max(0.0, start - 1.0) / stream.time_base), stream=stream)
        except Exception:
            pass
        got, first_time = 0, None
        for frame in container.decode(stream):
            if frame.time is not None and frame.time + frame.samples / frame.sample_rate < start:
                continue
            for rf in resampler.resample(frame) or []:
                data = rf.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
                if first_time is None:
                    first_time = rf.time if rf.time is not None else start
                out.append(data)
                got += len(data)
            if got >= need + int((start - (first_time or start)) * SAMPLE_RATE) + SAMPLE_RATE:
                break
    if not out:
        return np.zeros(0, np.float32)
    audio = np.concatenate(out)
    skip = max(0, int(round((start - (first_time or start)) * SAMPLE_RATE)))
    return audio[skip:skip + need]


EDGE_PAD = 8.0         # seconds looked at around the rough edges
EDGE_MATCH = 0.85      # columns this similar (smoothed over 1 s) are the same audio


def refine_places(places, max_airings: int = 6):
    """Exact start/end for a repeated clip: line up its airings 0.1 s by
    0.1 s and keep the stretch where they really match. Where the matching
    audio carries on past the window (e.g. a whole sign-off sequence that's
    identical every night), the rough edge is kept on that side."""
    places = list(places)
    if len(places) < 2:
        return places
    ref_path, ra, rb = places[0]
    r0 = max(0.0, ra - EDGE_PAD)
    ref_fp = fingerprint(decode_window(ref_path, r0, rb + EDGE_PAD))
    core_a = int(round((ra - r0) / FRAME_SECONDS))
    core_b = min(len(ref_fp), int(round((rb - r0) / FRAME_SECONDS)))
    core = ref_fp[core_a:core_b]
    if len(core) < 10:
        return places
    sims, shifts = [], {}
    for idx, (path, a, b) in enumerate(places[1:max_airings], start=1):
        o0 = max(0.0, a - EDGE_PAD)
        fp = fingerprint(decode_window(path, o0, b + EDGE_PAD))
        scores = _scores(fp, core)
        if not len(scores):
            continue
        # Only allow a line-up close to where the finder put it (a song's
        # chorus elsewhere in the window would also match).
        expect = int(round((a - o0) / FRAME_SECONDS))
        lo_k, hi_k = max(0, expect - 40), min(len(scores), expect + 41)
        if lo_k >= hi_k:
            continue
        k = lo_k + int(np.argmax(scores[lo_k:hi_k]))
        if scores[k] < MATCH_SCORE - 0.05:
            continue
        shift = k - core_a  # this airing's column = reference column + shift
        sim = np.full(len(ref_fp), np.nan, np.float32)
        lo_i, hi_i = max(0, -shift), min(len(ref_fp), len(fp) - shift)
        if hi_i > lo_i:
            sim[lo_i:hi_i] = np.einsum("ij,ij->i", ref_fp[lo_i:hi_i], fp[lo_i + shift:hi_i + shift])
        sims.append(sim)
        shifts[idx] = (o0, shift)
    if not sims:
        return places
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # columns no airing covers
        sim = np.nanmean(np.vstack(sims), axis=0)
    smooth = np.convolve(np.nan_to_num(sim, nan=0.0), np.ones(10) / 10, mode="same")
    mid = (core_a + core_b) // 2
    lo = hi = mid
    while lo > 0 and smooth[lo - 1] >= EDGE_MATCH:
        lo -= 1
    while hi < len(smooth) - 1 and smooth[hi + 1] >= EDGE_MATCH:
        hi += 1
    hi += 1
    # Ran into the edge of the window: the same audio continues, keep the
    # finder's edge on that side.
    if lo <= 12:  # within ~1 s of the window's edge
        lo = core_a
    if hi >= len(smooth) - 12:
        hi = core_b
    refined = [(ref_path, round(r0 + lo * FRAME_SECONDS, 1), round(r0 + hi * FRAME_SECONDS, 1))]
    for idx, (path, a, b) in enumerate(places[1:], start=1):
        if idx in shifts:
            o0, shift = shifts[idx]
            refined.append((path, round(o0 + (lo + shift) * FRAME_SECONDS, 1),
                            round(o0 + (hi + shift) * FRAME_SECONDS, 1)))
        else:  # not lined up: move it by the same amount as the reference
            refined.append((path, round(a + (refined[0][1] - ra), 1),
                            round(a + (refined[0][1] - ra) + (refined[0][2] - refined[0][1]), 1)))
    return refined


def update_text(output_folder, jingle_id: str, text: str) -> None:
    """Change the words of a saved item."""
    path = library_folder(output_folder) / f"{jingle_id}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["text"] = text.strip()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _cache.clear()
