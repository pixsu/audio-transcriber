# MP3 Transcriber

Transcribes every `.mp3` in a folder to a `.txt` file using
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), then keeps
watching the folder and transcribes new MP3s as they appear.

- Input: `C:\Users\Gelo\Documents\mp3` (only this folder, no subfolders)
- Output: `C:\Users\Gelo\Documents\transcribed` (created automatically)
- `meeting1.mp3` → `meeting1.txt`
- MP3s that already have a `.txt` are skipped, so re-running is safe.
- Original MP3s are never modified, moved or deleted.

## FFmpeg is not needed

faster-whisper decodes audio with **PyAV**, which ships its own copy of the
FFmpeg libraries inside the Python package (and so inside the .exe). You do
not need to install FFmpeg or put it on your PATH.

## Build the .exe

1. Install **64-bit Python 3.11 or 3.12** from python.org and tick
   **"Add python.exe to PATH"** during setup.
2. Double-click **`build.bat`**.
   It creates a `.venv`, installs `requirements.txt` and runs PyInstaller.
3. The result is **`dist\MP3Transcriber.exe`** (~150–250 MB, since it bundles
   Python and the AI libraries).

The PyInstaller command used (also in `build.bat`):

```bat
pyinstaller --noconfirm --clean --onefile --console --name MP3Transcriber ^
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

Double-click `MP3Transcriber.exe` (you can move it to any folder first).

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
    "poll_interval_seconds": 10,
    "device": "auto"
}
```

| Setting | Values |
|---|---|
| `model_size` | `tiny`, `base`, `small`, `medium`, `large-v3` (bigger = more accurate but slower) |
| `language` | `"auto"` to detect, or a code like `"en"` or `"tl"` (Tagalog) |
| `include_timestamps` | `true` gives lines like `[00:01:23 → 00:01:30] text here` |
| `vad_filter` | `true` skips silence (best for talks/meetings). Set `false` for **songs**: the filter treats singing over music as "not speech" and drops most of it |
| `poll_interval_seconds` | How often to check for new files |
| `device` | `"auto"`, `"cuda"` (GPU) or `"cpu"` |

Note: backslashes in paths must be doubled (`\\`) in JSON.

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
