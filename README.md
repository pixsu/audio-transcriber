# Audio Transcriber

A Windows tool that automatically turns audio and video files into text.
Drop a file into a folder and a `.txt` transcript appears in another folder.
It runs fully offline on your own PC, using
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (an optimized
version of OpenAI's Whisper speech-recognition model). No account, API key or
subscription is needed.

## Features

- **Hands-free:** processes everything in the input folder, then keeps
  watching it and transcribes new files as they arrive.
- **Many formats:** `.mp3`, `.mp4`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`,
  `.webm`, `.aac`, `.wma`, `.mkv`, `.mov`. Audio is extracted from video files
  automatically.
- **Safe:** original files are only read, never modified, moved or deleted.
  Files that already have a transcript are skipped, so re-running is safe.
- **Robust:** waits until a file has finished copying before starting, and a
  corrupted file is logged and skipped without stopping the program. Only
  one copy can watch a folder at a time, so files are never processed twice.
- **Accurate on music and speech:** uses the file name as a hint for names and
  song titles, and removes lines Whisper is known to invent (such as
  *"Thanks for watching!"*).
- **Works on modest hardware:** runs on a normal CPU; uses an NVIDIA GPU
  automatically if one is set up.
- **No FFmpeg install needed:** audio decoding is built in (via PyAV).

## Requirements

- Windows 10 or 11 (64-bit)
- 4 GB RAM minimum (8 GB+ recommended for the `medium` model)
- About 1–3 GB free disk space (program + one model)
- Internet only for the first run, to download the speech model
- To build from source: **64-bit Python 3.11 or 3.12**

## Getting started

### 1. Get the program

#### Option A: Download (easiest, no Python needed)

1. Go to the [**Releases**](https://github.com/pixsu/audio-transcriber/releases)
   page and download `AudioTranscriber-<version>-windows-x64.zip` from the
   latest release.
2. Unzip it anywhere (e.g. `Documents\AudioTranscriber`).
3. Continue with [Run it](#2-run-it). The program is
   `MP3Transcriber\MP3Transcriber.exe` inside the unzipped folder.

> The program is not code-signed, so Windows may show *"Windows protected
> your PC"* the first time. Click **More info → Run anyway**. Every release is
> built automatically from this repository's source code by GitHub Actions.

#### Option B: Build from source

1. Install **64-bit Python 3.11 or 3.12** from
   [python.org](https://www.python.org/downloads/). During setup, tick
   **"Add python.exe to PATH"**.
2. Download this repository (**Code → Download ZIP**, then unzip), or clone it:
   ```bash
   git clone https://github.com/pixsu/audio-transcriber.git
   ```
3. Double-click **`build.bat`**. It creates a virtual environment, installs the
   dependencies and builds the program. This takes a few minutes.

When it finishes you will have:

| Output | What it is |
|---|---|
| `dist\MP3Transcriber\` | The program folder. Run `MP3Transcriber.exe` inside it. |
| `dist\MP3Transcriber.zip` | A clean copy of that folder to give to other PCs (no Python needed there). |

> Keep `MP3Transcriber.exe` and the `_internal` folder next to it together.
> To move the program, move the whole `MP3Transcriber` folder.

### 2. Run it

1. Double-click `dist\MP3Transcriber\MP3Transcriber.exe`.
2. On the first run it:
   - creates two folders in **your Documents folder**:
     `Documents\mp3` (put files here) and `Documents\transcribed`
     (transcripts appear here);
   - creates a `config.json` settings file next to the .exe;
   - downloads the speech model (`small` ≈ 480 MB, `medium` ≈ 1.5 GB) into a
     `models` folder next to the .exe. After that it works offline.
3. Drop audio or video files into `Documents\mp3`. The console shows the
   progress:
   ```text
   Found 2 audio files
   [1/2] Transcribing meeting1.mp3...
      Detected language: en (98% sure), audio length 00:42:10
   Done → meeting1.txt (took 312s)
   Watching C:\Users\YourName\Documents\mp3 for new audio files every 10s...
   ```
4. Each file gets a transcript with the same name: `meeting1.mp3` →
   `meeting1.txt`. If two files share a name (e.g. `song.mp3` and
   `song.mp4`), the second one gets `song.mp4.txt` so neither is skipped or
   overwritten. (This is tracked in a small hidden file,
   `.transcriber-index.json`, in the output folder.)
5. To stop, press **Ctrl+C** or close the window.

Tip: right-click `MP3Transcriber.exe` → *Send to → Desktop (create shortcut)*.
To start it with Windows, put that shortcut in the Startup folder (press
`Win + R` and type `shell:startup`).

### 3. Use different folders (optional)

To use other folders, close the program, open `config.json` in Notepad and
change `input_folder` and `output_folder`. Use double backslashes:

```json
"input_folder": "D:\\Recordings",
"output_folder": "D:\\Recordings\\Transcripts",
```

Save the file and start the program again. Missing folders are created
automatically.

## Configuration

All settings live in `config.json` next to the .exe. Restart the program after
changing them.

```json
{
    "input_folder": "C:\\Users\\YourName\\Documents\\mp3",
    "output_folder": "C:\\Users\\YourName\\Documents\\transcribed",
    "model_size": "small",
    "language": "auto",
    "include_timestamps": false,
    "vad_filter": true,
    "use_filename_hint": true,
    "file_types": [".mp3", ".mp4", ".m4a", ".wav", ".flac", ".ogg", ".opus",
                   ".webm", ".aac", ".wma", ".mkv", ".mov"],
    "poll_interval_seconds": 10,
    "device": "auto"
}
```

| Setting | Default | Description |
|---|---|---|
| `input_folder` | `Documents\mp3` | Folder to read audio/video files from. Subfolders are not scanned. Created if missing. |
| `output_folder` | `Documents\transcribed` | Folder to save `.txt` transcripts to. Created if missing. |
| `model_size` | `small` | `tiny`, `base`, `small`, `medium` or `large-v3`. Larger is more accurate but slower and uses more memory. See [Choosing a model](#choosing-a-model). |
| `language` | `auto` | `auto` detects the language. Or force one with a code, e.g. `en` (English) or `tl` (Tagalog). |
| `include_timestamps` | `false` | `true` writes lines like `[00:01:23 → 00:01:30] text here`. |
| `vad_filter` | `true` | Skips silence. Good for speech; **set to `false` for music** (see below). |
| `use_filename_hint` | `true` | Passes the cleaned file name (e.g. `Artist Song Title`) to the model to help it spell names and titles. |
| `file_types` | (list above) | File extensions to pick up. |
| `poll_interval_seconds` | `10` | How often to check the input folder for new files. |
| `device` | `auto` | `auto` uses an NVIDIA GPU if available, otherwise the CPU. Force with `cuda` or `cpu`. |

### Recommended settings

| Use case | `model_size` | `vad_filter` | Notes |
|---|---|---|---|
| Meetings, lectures, podcasts | `small` or `medium` | `true` | Silence is skipped, which saves time. |
| Songs and music | `medium` | `false` | The silence filter treats singing over music as "not speech" and would drop most of the song. |
| Low-end PC | `base` or `small` | either | `small` is a good balance of speed and accuracy. |

If the silence filter removes more than half of a file, the program prints a
warning suggesting `"vad_filter": false`.

### Choosing a model

Speed on a 4-minute song, measured on an Intel Core i7-12700F (CPU only):

| Model | Download size | Time | Accuracy |
|---|---|---|---|
| `small` | ~480 MB | ~30 s | Good. Best choice for low-end PCs. |
| `medium` | ~1.5 GB | ~70–100 s | Better, especially for lyrics and non-English audio. |

Slower CPUs will take proportionally longer. `large-v3-turbo` is not
recommended for music: in testing it produced placeholder text instead of
lyrics.

### Tips for better accuracy

- **Name files descriptively.** `Artist - Song Title.mp3` gives the model a
  useful hint. Downloader junk such as `ytmp4`, `savetube` or
  `(Official Video)` is removed automatically.
- **Use `medium`** if your PC can handle it.
- **Set `language`** if all your files are in one language and auto-detect
  gets it wrong.
- **Never force `"en"` for non-English audio.** Whisper may translate it into
  English instead of transcribing it.

## Language support

Whisper supports about 99 languages. Philippine languages in particular:

| Language | Support | Notes |
|---|---|---|
| English | Excellent | |
| Tagalog / Filipino | Good | Works with `auto` or `"tl"`. Use `medium` for best results. Taglish (mixed Tagalog and English) works with `auto`. |
| Cebuano / Bisaya, Ilocano, Hiligaynon, Waray | Partial | Not in Whisper's language list. Expect a rough draft: shared and borrowed words are usually correct, but language-specific words may be misspelled. Try both `auto` and `"tl"` and keep whichever reads better. |

## GPU acceleration (optional)

With `"device": "auto"`, an NVIDIA GPU is used automatically if the required
libraries are present. The startup message shows what was picked:
`Model loaded on GPU (CUDA, float16)` or `Model loaded on CPU (int8)`.

The GPU needs **CUDA 12 cuBLAS** and **cuDNN 9** plus an up-to-date NVIDIA
driver. They are not bundled with the program. Either:

- **Easiest:** download the CUDA 12 cuBLAS + cuDNN pack from
  [Purfview/whisper-standalone-win](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs)
  and extract the `.dll` files next to `MP3Transcriber.exe` (or into a `cuda`
  folder next to it), **or**
- install the [CUDA Toolkit 12](https://developer.nvidia.com/cuda-downloads)
  and [cuDNN 9](https://developer.nvidia.com/cudnn) and add their `bin`
  folders to your PATH.

The required files are `cublas64_12.dll`, `cublasLt64_12.dll` and the
`cudnn*64_9.dll` files. If they are missing, the program falls back to the CPU
and keeps working.

## Troubleshooting

| Problem | Solution |
|---|---|
| A file is ignored | Check its real extension: Windows hides extensions by default, so a `.mp4` can look like an MP3. Enable *View → Show → File name extensions* in Explorer and make sure the extension is in `file_types`. |
| Only part of a song is transcribed | Set `"vad_filter": false`. |
| `config.json is not valid JSON` | Paths need double backslashes (`C:\\Users\\...`), and every line except the last needs a comma. |
| A file failed | The error is shown in the console and saved in `transcriber.log`. Failed files are not retried unless the file changes; restart the program to retry. |
| "Another copy of the transcriber is already watching..." | The program is already running (check the taskbar). Close that window first. |
| A transcript should be redone | Delete its `.txt` file; it is transcribed again within one check. |
| Very slow | Use a smaller `model_size`, or set up a GPU. |
| `GPU could not be used` | The CUDA libraries are missing; see [GPU acceleration](#gpu-acceleration-optional). |
| Antivirus warning | Unsigned programs built with PyInstaller are sometimes flagged by mistake. Building it yourself from this source avoids trusting a third-party binary. |
| Transcript has odd lines in instrumental parts | Whisper sometimes guesses words during music. Common fake lines are removed automatically; others can be deleted by hand. |

Logs are written to `transcriber.log` next to the .exe, including full error
details. The log is capped at 1 MB; older entries move to
`transcriber.log.1` to `.3`, so it never grows without limit.

## Running from source

After running `build.bat` once (to create `.venv`), you can run the script
directly without building:

```bat
.venv\Scripts\python.exe transcriber.py
```

When run this way, `config.json`, `transcriber.log` and `models\` are created
next to `transcriber.py`.

## Project structure

```text
transcriber.py          The application
requirements.txt        Top-level Python dependencies (loose versions)
requirements-lock.txt   Exact tested versions, used by build.bat
build.bat               One-click build script (venv + install + PyInstaller)
README.md               This file
CHANGELOG.md            What changed in each version
LICENSE                 MIT license
.github/workflows/      Automatic build, test and release on GitHub
.gitignore              Keeps build output, models and personal settings out of git
.gitattributes          Keeps build.bat in Windows line endings
```

Generated (not in git): `.venv\`, `build\`, `dist\`, and at runtime
`config.json`, `transcriber.log` and `models\`.

## Build details

`build.bat` builds into `build\staging` first and then copies the program files
into `dist\MP3Transcriber`, so rebuilding never deletes your downloaded
models, settings or log. It runs:

```bat
pyinstaller --noconfirm --clean --onedir --console --name MP3Transcriber ^
    --distpath build\staging ^
    --collect-all faster_whisper --collect-all ctranslate2 ^
    --collect-all onnxruntime --collect-all av --collect-all tokenizers ^
    --copy-metadata faster-whisper --copy-metadata huggingface_hub ^
    --copy-metadata tokenizers --copy-metadata tqdm --copy-metadata numpy ^
    transcriber.py
```

- `--collect-all` bundles the DLLs, compiled modules and data files (such as
  the Silero VAD model) that PyInstaller cannot detect on its own.
- `--copy-metadata` keeps package version information some libraries check at
  runtime.
- `--onedir` (a folder) is used instead of `--onefile`: a single-file .exe
  unpacks itself to the Temp folder on every start, which is slower and leaves
  large leftovers behind if the program is closed abruptly.
- Dependencies are installed from `requirements-lock.txt` (exact versions), so
  every build uses the same tested packages. A new release of any library
  can't silently break the build. For example, PyAV 15+ breaks
  faster-whisper's audio loading, so it is held at 14.2.0.
- To update dependencies: install `requirements.txt` into a fresh `.venv`, test
  the app, then regenerate the lock file with
  `.venv\Scripts\python.exe -m pip freeze > requirements-lock.txt`
  (remove the `pip` and `setuptools` lines).

## Releases (for maintainers)

Builds run automatically on GitHub (free) using
[`.github/workflows/build.yml`](.github/workflows/build.yml):

- **Every push to `main`** that changes the app builds it, runs a quick test
  (the built .exe transcribes a short test file), and keeps the zip for 30
  days under *Actions → the run → Artifacts*.
- **Publishing a release:**
  1. Bump `__version__` in `transcriber.py` (e.g. `1.0.1`).
  2. Add a section for it at the top of `CHANGELOG.md`.
  3. Commit and push, then tag and push the tag:
     ```bash
     git tag v1.0.1
     git push origin v1.0.1
     ```
  The workflow checks the tag matches `__version__`, builds and tests the
  program, and creates a GitHub Release with
  `AudioTranscriber-1.0.1-windows-x64.zip` attached and the changelog section
  as release notes.

## License

This project is licensed under the [MIT License](LICENSE).

The release download bundles third-party components under their own licenses,
including faster-whisper and CTranslate2 (MIT), PyAV (BSD), FFmpeg libraries
inside PyAV (LGPL), ONNX Runtime (MIT) and Python (PSF). The Whisper models
downloaded on first run are released by OpenAI under the MIT License.

## Acknowledgements

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) by SYSTRAN
- [Whisper](https://github.com/openai/whisper) by OpenAI
- [CTranslate2](https://github.com/OpenNMT/CTranslate2),
  [PyAV](https://github.com/PyAV-Org/PyAV) and
  [PyInstaller](https://pyinstaller.org/)
