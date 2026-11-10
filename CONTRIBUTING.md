# Contributing

Thanks for helping improve Audio Transcriber. Bug reports, ideas and pull
requests are all welcome.

## Reporting problems and ideas

Use the [issue templates](https://github.com/pixsu/audio-transcriber/issues/new/choose).
For bugs, the version (shown at the top of the window), the settings you
used and a few lines from the log (**Menu → Open log file**) help a lot.
Please don't attach recordings or transcripts with private content.

## Running from source

Requirements: Windows 10/11 and 64-bit **Python 3.11 or 3.12**.

```bat
git clone https://github.com/pixsu/audio-transcriber.git
cd audio-transcriber
build.bat
```

`build.bat` creates `.venv`, installs the exact tested package versions from
`requirements-lock.txt` and builds the program. After that you can run the
code directly:

```bat
.venv\Scripts\python.exe app.py               (window)
.venv\Scripts\python.exe transcriber.py       (console mode)
```

When run from source, `config.json`, `transcriber.log` and `models\` are
created next to the scripts (all ignored by git). Tip: point
`input_folder` / `output_folder` in that `config.json` at test folders and
use `"model_size": "tiny"` for quick tests.

## Code layout

| File | What's in it |
|---|---|
| `transcriber.py` | The engine: settings, model loading, folder watching, queue, multi-PC claims, transcription. Runs on its own in a console. |
| `jingles.py` | Jingle library: sound fingerprints of repeating jingles/ads and matching them in new files. |
| `music_detect.py` | Music detection (YAMNet sound classifier) for the `[MUSIC]` labels. |
| `app.py` | The window (CustomTkinter), tray icon, settings panel, update check. Talks to the engine through the `Hooks` class. |
| `build.bat` | Builds the program folder, the zip and (with Inno Setup 6) the installer. |
| `installer.iss` | Inno Setup script for `AudioTranscriber-Setup.exe`. |
| `.github/workflows/build.yml` | Builds and tests every push; publishes releases from `v*` tags. |

## Guidelines

- Keep it easy to use: settings in plain language, errors that say what to
  do, nothing that needs editing files by hand.
- Keep it free and offline: no paid services, and no data leaves the PC.
- Keep it light: it should still run on ordinary office PCs.
- Never modify, move or delete the user's original audio files.
- Comments explain *why*, in plain English.
- If you change dependencies, update `requirements.txt` and regenerate
  `requirements-lock.txt` (see the README's *Build details*).

## Pull requests

1. Fork the repository and create a branch.
2. Make your change and test it (from source, and with `build.bat` if it
   touches packaging).
3. Update the README and add a `CHANGELOG.md` entry for user-facing changes.
4. Open a pull request describing what changed and how you tested it.

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
