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
    # Its sound codes, so new recordings can be checked against every saved
    # item in one quick lookup.
    try:
        c, l = make_codes(clip)
        np.savez(folder / f"{base}.codes.npz", codes=c[l], pos=np.flatnonzero(l).astype(np.int32))
    except Exception as e:
        log.warning("Couldn't save the sound codes: %s", e)
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
            codes = None
            cpath = meta_path.with_name(meta_path.stem + ".codes.npz")
            if cpath.exists():
                with np.load(cpath, allow_pickle=False) as z:
                    codes = (z["codes"], z["pos"])
            items.append(dict(meta, id=meta_path.stem, fp=fp, codes=codes,
                              preview=str(wav) if wav.exists() else None))
        except (OSError, ValueError) as e:
            log.warning("Skipping damaged jingle %s (%s)", meta_path.name, e)
    _cache.update(key=key, items=items)
    return items


def delete_jingle(output_folder, jingle_id: str) -> None:
    folder = library_folder(output_folder)
    for ext in (".json", ".npy", ".wav", ".codes.npz"):
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

CODE_SMOOTH = 30          # each code describes 0.3 s of sound...
CODE_LAG = 15             # ...compared with 0.15 s earlier
CODE_EVERY = 4            # one code kept every 40 ms (about 350 KB per hour)
CODE_STEP = 0.01 * CODE_EVERY
REPEAT_MIN_VOTES = 15     # matching codes needed for a stretch to count
REPEAT_NEIGHBOURS = 11    # each place is compared with this many others with the same code
STORE_FOLDER = ".fingerprints"


def make_codes(audio: np.ndarray):
    """Compact sound codes for spotting repeated audio: one 32-bit code
    every 40 ms (how the loudness of neighbouring frequency bands changes),
    and which of them are not dead air."""
    mel = music_detect.log_mel(audio)
    k = CODE_SMOOTH
    if len(mel) <= k + CODE_LAG:
        return np.zeros(0, np.uint32), np.zeros(0, bool)
    c = np.cumsum(np.vstack([np.zeros((1, mel.shape[1]), np.float32), mel]), axis=0)
    sm = (c[k:] - c[:-k]) / k
    edges = np.linspace(0, mel.shape[1], 34).astype(int)
    bands = np.stack([sm[:, edges[i]:edges[i + 1]].mean(axis=1) for i in range(33)], axis=1)
    d = bands[:, :-1] - bands[:, 1:]
    bits = (d[CODE_LAG:] - d[:-CODE_LAG]) > 0
    codes = (bits.astype(np.uint64) << np.arange(32, dtype=np.uint64)).sum(axis=1)
    loud = sm[CODE_LAG:].mean(axis=1) > -8
    return codes.astype(np.uint32)[::CODE_EVERY], loud[::CODE_EVERY]


# --------------------------------------------------------------- stored codes
# Every transcribed recording leaves its codes in .fingerprints in the
# Transcripts folder, so repeats can be found across all of them later -
# even after the original recordings are gone - without reading any audio.

def _store_name(audio_name: str) -> str:
    return re.sub(r"[^\w\-. ]+", "_", audio_name)[:150] + ".npz"


def store_codes(output_folder, audio_path, audio: np.ndarray = None, codes=None) -> bool:
    """Save the codes of one recording (decoding it if `audio` isn't given;
    `codes` = (codes, loud) if they were already worked out)."""
    folder = Path(output_folder) / STORE_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    if codes is None:
        if audio is None:
            from faster_whisper.audio import decode_audio
            audio = decode_audio(str(audio_path))
        codes = make_codes(audio)
    codes, loud = codes
    seconds = len(audio) / SAMPLE_RATE if audio is not None else len(loud) * CODE_STEP
    try:
        recorded = Path(audio_path).stat().st_mtime
    except OSError:
        recorded = time.time()
    target = folder / _store_name(Path(audio_path).name)
    tmp = target.with_name(target.stem + ".tmp.npz")
    np.savez(tmp, codes=codes, loud=np.packbits(loud), n=len(loud),
             name=Path(audio_path).name, recorded=recorded, seconds=seconds)
    os.replace(tmp, target)
    # The file's date = the recording's date, so the newest recordings can be
    # picked by date alone, without opening every file.
    try:
        os.utime(target, (recorded, recorded))
    except OSError:
        pass
    return True


def has_codes(output_folder, audio_name: str) -> bool:
    return (Path(output_folder) / STORE_FOLDER / _store_name(audio_name)).exists()


def _date_old_files(folder: Path) -> None:
    """One time only: files saved by v1.25 have the date they were saved,
    not the recording's date. Fix them so picking the newest works."""
    marker = folder / ".dated"
    if marker.exists() or not folder.is_dir():
        return
    for f in folder.glob("*.npz"):
        try:
            with np.load(f, allow_pickle=False) as z:
                recorded = float(z["recorded"])
            os.utime(f, (recorded, recorded))
        except Exception:
            pass
    try:
        marker.write_text("file dates = recording dates", encoding="utf-8")
    except OSError:
        pass


def load_codes(output_folder, limit: int = 200):
    """[(name, codes, loud), ...] for the newest `limit` stored recordings."""
    folder = Path(output_folder) / STORE_FOLDER
    entries = []
    _date_old_files(folder)
    try:
        dated = [(f.stat().st_mtime, f) for f in folder.glob("*.npz") if ".tmp" not in f.name]
    except OSError:
        return []
    # Newest first by file date (= recording date); open only those needed.
    dated.sort(key=lambda d: -d[0])
    for _, f in dated[:limit]:
        try:
            with np.load(f, allow_pickle=False) as z:
                n = int(z["n"])
                entries.append((float(z["recorded"]), str(z["name"]), z["codes"],
                                np.unpackbits(z["loud"])[:n].astype(bool)))
        except Exception as e:
            log.debug("Skipping %s (%s)", f.name, e)
    entries.sort(key=lambda e: -e[0])
    return [(name, codes, loud) for _, name, codes, loud in entries[:limit]]


# --------------------------------------------------------------- repeats

def find_repeats(entries, min_seconds=4.0, max_seconds=900.0, should_stop=None):
    """Stretches of audio that air more than once across recordings.

    entries: [(recording name, codes, loud), ...] (see load_codes).
    Returns [{"seconds", "count", "places": [(name, start, end), ...]}, ...],
    most useful first (aired most often, then longest)."""
    from collections import defaultdict
    if len(entries) < 1:
        return []
    names = [e[0] for e in entries]
    codes = np.concatenate([c[m] for _, c, m in entries])
    fid = np.concatenate([np.full(int(m.sum()), i, np.int32) for i, (_, c, m) in enumerate(entries)])
    pos = np.concatenate([np.flatnonzero(m).astype(np.int32) for _, c, m in entries])
    if not len(codes):
        return []
    order = np.argsort(codes, kind="stable")  # keeps (recording, time) order within a code
    codes, fid, pos = codes[order], fid[order], pos[order]
    cuts = np.flatnonzero(np.diff(codes)) + 1
    gid = np.zeros(len(codes), np.int64)
    gid[cuts] = 1
    gid = np.cumsum(gid)
    sizes = np.bincount(gid)
    common = sizes[gid] > 3000  # a code that's everywhere (hum, tone) says nothing
    # Each place is paired with the next few places with the same code.
    fa_l, fb_l, qa_l, qb_l = [], [], [], []
    for d in range(1, REPEAT_NEIGHBOURS + 1):
        same = (gid[:-d] == gid[d:]) & ~common[:-d]
        if not same.any():
            break
        i = np.flatnonzero(same)
        fa, fb, qa, qb = fid[i], fid[i + d], pos[i], pos[i + d]
        keep = ~((fa == fb) & (qb - qa < int(120 / CODE_STEP)))  # not a song's own chorus
        fa_l.append(fa[keep]); fb_l.append(fb[keep]); qa_l.append(qa[keep]); qb_l.append(qb[keep])
    if should_stop:
        should_stop()
    if not fa_l:
        return []
    fa, fb, qa, qb = (np.concatenate(x) for x in (fa_l, fb_l, qa_l, qb_l))
    bin_size = max(1, int(round(0.1 / CODE_STEP)))
    off = (qb.astype(np.int64) - qa) // bin_size
    key = (fa.astype(np.int64) * 100_000 + fb) * 1_000_000 + (off + 500_000)
    uniq, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    strong = np.flatnonzero(counts >= REPEAT_MIN_VOTES)
    places, links = [], []
    gap = int(3 / CODE_STEP)
    tail = int(0.3 / CODE_STEP)
    for u in strong:
        k = int(uniq[u])
        offset = (k % 1_000_000 - 500_000) * bin_size * CODE_STEP
        pair = k // 1_000_000
        a_file, b_file = pair // 100_000, pair % 100_000
        qs = np.sort(qa[inv == u])
        for g in np.split(qs, np.flatnonzero(np.diff(qs) > gap) + 1):
            if len(g) < REPEAT_MIN_VOTES:
                continue
            a0, a1 = g[0] * CODE_STEP, (g[-1] + tail) * CODE_STEP
            if not min_seconds <= a1 - a0 <= max_seconds:
                continue
            places.append((a_file, a0, a1))
            places.append((b_file, a0 + offset, a1 + offset))
            links.append((len(places) - 2, len(places) - 1))
    if not places:
        return []
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
        airings = []
        for f, a, b in sorted(members):
            if airings and airings[-1][0] == f and a < airings[-1][2] + 60:  # one airing
                airings[-1] = (f, airings[-1][1], max(b, airings[-1][2]))
            else:
                airings.append((f, a, b))
        if len(airings) < 2:
            continue
        results.append(airings)
    # Pieces of one longer stretch that always airs together (e.g. a whole
    # sign-off sequence) come out as several clips side by side: join them.
    merged = True
    while merged:
        merged = False
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                A, B = results[i], results[j]
                if len(A) != len(B) or sorted(f for f, _, _ in A) != sorted(f for f, _, _ in B):
                    continue
                pairs = list(zip(sorted(A), sorted(B)))
                if all(fa == fb and min(b1, b2) - max(a1, a2) > -15
                       for (fa, a1, b1), (fb, a2, b2) in pairs):
                    results[i] = [(fa, min(a1, a2), max(b1, b2))
                                  for (fa, a1, b1), (_, a2, b2) in pairs]
                    del results[j]
                    merged = True
                    break
            if merged:
                break
    out = []
    for airings in results:
        seconds = float(np.median([b - a for _, a, b in airings]))
        if seconds > max_seconds:
            continue
        out.append({"seconds": round(seconds, 1), "count": len(airings),
                    "places": [(names[f], round(float(max(0.0, a)), 1), round(float(b), 1))
                               for f, a, b in airings]})
    out.sort(key=lambda r: (-r["count"], -r["seconds"]))
    return out


def known_in_library(result, audio_lookup, library) -> bool:
    """True when a repeat is already saved (checked on one airing whose
    recording is still available)."""
    if not library:
        return False
    for name, a, b in result["places"]:
        path = audio_lookup(name)
        if not path:
            continue
        try:
            clip = decode_window(path, max(0.0, a - 2), b + 2)
        except Exception:
            continue
        for ja, jb, _item, _off in find_jingles(clip, library):
            if min(jb, b - a + 2) - max(ja, 2) > 0.5 * (b - a):
                return True
        return False
    return False


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



# --------------------------------------------------------------- fast lookup

LOOKUP_MIN_VOTES = 8   # matching codes needed before a saved item is checked properly


def find_jingles_fast(audio: np.ndarray, library: list, file_codes=None, should_stop=None) -> list:
    """Same result as find_jingles, but quick with a big library: every saved
    item that has sound codes is looked up at once (one sort), and only the
    few that line up somewhere get the precise 8-second-piece check, on just
    that stretch. Items saved before this existed use the old way."""
    with_codes = [j for j in library if j.get("codes") is not None and len(j["codes"][0])]
    without = [j for j in library if j not in with_codes]
    found = find_jingles(audio, without, should_stop) if without else []
    if not with_codes:
        return found
    if file_codes is None:
        file_codes = make_codes(audio)
    codes, loud = file_codes
    fpos = np.flatnonzero(loud).astype(np.int64)
    fcodes = codes[loud]
    # All saved items' codes in one sorted table.
    lib_codes = np.concatenate([j["codes"][0] for j in with_codes])
    lib_item = np.concatenate([np.full(len(j["codes"][0]), i, np.int64)
                               for i, j in enumerate(with_codes)])
    lib_pos = np.concatenate([j["codes"][1].astype(np.int64) for j in with_codes])
    order = np.argsort(lib_codes, kind="stable")
    lib_codes, lib_item, lib_pos = lib_codes[order], lib_item[order], lib_pos[order]
    lo = np.searchsorted(lib_codes, fcodes, side="left")
    hi = np.searchsorted(lib_codes, fcodes, side="right")
    hit = hi > lo
    if not hit.any():
        return found
    # Each matching code votes for (item, where the item would start here).
    reps = (hi - lo)[hit]
    starts = np.repeat(lo[hit], reps) + (np.arange(reps.sum()) - np.repeat(np.cumsum(reps) - reps, reps))
    file_at = np.repeat(fpos[hit], reps)
    item = lib_item[starts]
    begin = (file_at - lib_pos[starts]) // 25          # 1-second bins
    key = item * 10_000_000 + (begin + 5_000_000)
    uniq, counts = np.unique(key, return_counts=True)
    candidates = {}
    for k, n in zip(uniq[counts >= LOOKUP_MIN_VOTES], counts[counts >= LOOKUP_MIN_VOTES]):
        i, b = int(k // 10_000_000), int(k % 10_000_000 - 5_000_000)
        candidates.setdefault(i, []).append(b * 25 * CODE_STEP)
    hits = []
    for i, begins in candidates.items():
        if should_stop:
            should_stop()
        j = with_codes[i]
        length = float(j.get("seconds") or len(j["fp"]) * FRAME_SECONDS)
        for b in sorted(set(round(x) for x in begins)):
            a0 = max(0.0, b - 6.0)
            a1 = min(len(audio) / SAMPLE_RATE, b + length + 6.0)
            window = audio[int(a0 * SAMPLE_RATE):int(a1 * SAMPLE_RATE)]
            for a, e, it, off in find_jingles(window, [j]):
                hits.append((a + a0, e + a0, it, off))
    # Merge with the old-way results; drop overlaps (the longer match wins).
    chosen = []
    for a, e, it, off in sorted(found + hits, key=lambda h: -(h[1] - h[0])):
        if all(e <= ca or a >= ce for ca, ce, *_ in chosen):
            chosen.append((a, e, it, off))
    return sorted(chosen, key=lambda c: c[0])
