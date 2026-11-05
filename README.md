# MP3 Transcriber

Transcribes every audio file in a folder to a `.txt` file using
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), then keeps
watching the folder and transcribes new files as they appear.

- Input: `C:\Users\Gelo\Documents\mp3` (only this folder, no subfolders)
- Output: `C:\Users\Gelo\Documents\transcribed` (created automatically)
- `meeting1.mp3` → `meeting1.txt`
- Works with `.mp3`, and also `.mp4`, `.m4a`, `.wav`, `.flac`, `.ogg`,
  `.opus`, `.webm`, `.aac`, `.wma`, `.mkv`, `.mov` (audio is taken from videos).
- Files that already have a `.txt` are skipped, so re-running is safe.
- Original files are never modified, moved or deleted.

## FFmpeg is not needed

faster-whisper decodes audio with **PyAV**, which ships its own copy of the
FFmpeg libraries inside the Python package (and so inside the .exe). You do
not need to install FFmpeg or put it on your PATH.

## Build the .exe

1. Install **64-bit Python 3.11 or 3.12** from python.org and tick
   **"Add python.exe to PATH"** during setup.
2. Double-click **`build.bat`**.
   It creates a `.venv`, installs `requirements.txt` and runs PyInstaller.
3. You get:
   - **`dist\MP3Transcriber\`**: the program folder (~250 MB). Run
     `MP3Transcriber.exe` inside it. The `_internal` folder next to it holds
     Python and the AI libraries; keep the two together.
   - **`dist\MP3Transcriber.zip`** (~100 MB): a clean copy of that folder
     for other PCs, without your models or settings.

Rebuilding only replaces the program files. Your `models`, `config.json`
and `transcriber.log` in `dist\MP3Transcriber\` are kept.

**Why a folder and not a single .exe?** A single-file .exe unpacks ~250 MB
into the Temp folder every time it starts, and if it's closed abruptly that
copy is left behind (it piled up to 1.7 GB during testing). The folder
version starts faster, leaves nothing in Temp, and antivirus programs are
less likely to flag it.

The PyInstaller command used (also in `build.bat`, which builds into
`build\staging` first and then copies the result into `dist`):

```bat
pyinstaller --noconfirm --clean --onedir --console --name MP3Transcriber ^
    --collect-all faster_whisper --collect-all ctranslate2 ^
    --collect-all onnxruntime --collect-all av --collect-all tokenizers ^
    --copy-metadata faster-whisper --copy-metadata huggingface_hub ^
    --copy-metadata tokenizers --copy-metadata tqdm --copy-metadata numpy ^
    transcriber.py
```

Why these flags: `faster_whisper` contains the Silero VAD model file used by
`vad_filter`; `ctranslate2`, `onnxruntime`, `av` and `tokenizers` contain DLLs
and compiled modules PyInstaller won't find on its own; `--copy-metadata`
keeps package version info that some libraries check at runtime.

## Run

Double-click `dist\MP3Transcriber\MP3Transcriber.exe`. To put it somewhere
else, move the **whole `MP3Transcriber` folder**, not just the .exe. On
another PC, unzip `MP3Transcriber.zip` and run the .exe inside. Tip: right-click
the .exe → *Send to → Desktop (create shortcut)* for easy access.

- **First run** downloads the model (small ≈ 500 MB) into a `models` folder
  next to the .exe. Later runs work **offline**.
- `config.json` and `transcriber.log` are also created next to the .exe.
- Press **Ctrl+C** or close the window to stop.

To run without building: `.venv\Scripts\python.exe transcriber.py`

## Change settings

Edit `config.json` next to the .exe, then restart the program:

```json
{
    "input_folder": "C:\\Users\\Gelo\\Documents\\mp3",
    "output_folder": "C:\\Users\\Gelo\\Documents\\transcribed",
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

| Setting | Values |
|---|---|
| `model_size` | `tiny`, `base`, `small`, `medium`, `large-v3`. Bigger = more accurate but slower. **`small`** for low-end PCs (about 8× faster than real time on a modern CPU); **`medium`** for best accuracy on a decent PC (about 3× faster than real time, ~2 GB RAM). Avoid `large-v3-turbo` for songs: in testing it wrote "To be continued..." instead of lyrics |
| `use_filename_hint` | `true` passes the cleaned file name (e.g. `Olivia Rodrigo drop dead`) to the model as a hint, which helps it get names and song titles right. Junk like `ytmp4`, `savetube`, `(Official Video)` is removed first |
| `file_types` | Which file extensions to pick up |
| `language` | `"auto"` to detect, or a code like `"en"` or `"tl"` (Tagalog) |
| `include_timestamps` | `true` gives lines like `[00:01:23 → 00:01:30] text here` |
| `vad_filter` | `true` skips silence (best for talks/meetings). Set `false` for **songs**: the filter treats singing over music as "not speech" and drops most of it |
| `poll_interval_seconds` | How often to check for new files |
| `device` | `"auto"`, `"cuda"` (GPU) or `"cpu"` |

Note: backslashes in paths must be doubled (`\\`) in JSON.

Whisper sometimes invents lines like *"Thanks for watching!"* or *"To be
continued..."* during instrumental parts (it learned them from YouTube
subtitles). The program removes lines that are exactly one of these.

If a file fails (corrupted or unreadable), the error is logged and the
program moves on. It won't retry that file unless the file changes; to force
a retry, restart the program.

## Using an NVIDIA GPU

With `"device": "auto"` the program uses the GPU (`float16`) if it can,
otherwise the CPU (`int8`). The startup message says which one it picked.
If it says *"GPU could not be used"*, the CUDA libraries are missing.

Current faster-whisper versions need **CUDA 12 cuBLAS** and **cuDNN 9**
(plus an up-to-date NVIDIA driver). These are **not** bundled in the .exe.
Pick one way to add them:

- **Easiest:** download the cuBLAS + cuDNN DLL pack for CUDA 12 from
  [Purfview/whisper-standalone-win releases](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs)
  and extract the `.dll` files **next to `MP3Transcriber.exe`** (or into a
  `cuda` folder next to it).
- **Or** install the [CUDA Toolkit 12](https://developer.nvidia.com/cuda-downloads)
  and [cuDNN 9](https://developer.nvidia.com/cudnn) and make sure their `bin`
  folders are on your PATH.

The needed files are `cublas64_12.dll`, `cublasLt64_12.dll` and the
`cudnn*64_9.dll` files. Then restart the program; it should print
`Model loaded on GPU (CUDA, float16)`.
