# Changelog

All notable changes to this project are listed here.
Versions follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH` (breaking change / new feature / bug fix).

## [1.4.0] - 2026-11-06

### Added
- **A proper window** instead of a console: live status, a progress bar
  with time left, files waiting and done today, recent transcripts with an
  **Open** button, and a live Activity log.
- **Easy switches for the settings:** Fast / Accurate, quality (model),
  language, order, skip silence, timestamps, file-name hint. Changes are
  saved to `config.json` and apply from the next file, no restart needed.
  Changing the model loads the new one automatically.
- **Change the Inbox or Transcripts folder** with a folder picker.
- **Add files** by dragging them onto the window or with
  **Add audio files...** (they are copied into the Inbox).
- **Pause / Resume**, also mid-file.
- **Skip this file:** abandon the file being transcribed (after a
  confirmation) and move on to the next one. It is not retried until the
  file changes or the program restarts.
- **System tray icon:** closing the window keeps it running in the
  background; Windows notifications when a transcript is ready or a file
  fails; **Quit** from the tray menu.
- **Start with Windows** switch (starts hidden in the tray).
- An app icon.

### Changed
- `AudioTranscriber.exe` now opens the window. The text-only mode is still
  available with `AudioTranscriber.exe --console`.

### Upgrading from 1.3.0
- Unzip the new version and copy `config.json` and the `models` folder from
  the old folder into the new one. Nothing else changes.

## [1.3.0] - 2026-11-06

### Changed
- Renamed the program from `MP3Transcriber` to **`AudioTranscriber`**
  (`AudioTranscriber.exe` in an `AudioTranscriber` folder), since it handles
  far more than MP3s.
- New default folders, grouped under one parent folder:
  `Documents\Audio Transcriber\Inbox` (drop files here) and
  `Documents\Audio Transcriber\Transcripts` (transcripts appear here).
- Existing installs whose `config.json` names its own folders keep using
  them; nothing moves automatically.

### Upgrading from 1.2.0 or earlier
- Unzip the new version and copy `config.json` and the `models` folder from
  the old `MP3Transcriber` folder into the new `AudioTranscriber` folder.
- To use the new folder names, move your files into the new folders and
  update `input_folder` / `output_folder` in `config.json`. Move the hidden
  `.transcriber-index.json` along with the transcripts.

## [1.2.0] - 2026-11-06

### Added
- New `speed` setting: `fast` (default) or `accurate`. `fast` uses greedy
  decoding (beam size 1) and is about 35% faster: with the `medium` model a
  1-hour recording takes ~19 minutes instead of ~29 on an i7-12700F, with no
  accuracy loss on clear speech in testing. `accurate` keeps beam size 5.
- The startup line shows the speed, e.g. `Model: medium (fast)`.

### Changed
- README: speed/accuracy table for all setups and updated recommendations.

## [1.1.0] - 2026-11-06

### Added
- **Newest files first:** new `order` setting (`newest_first` by default,
  or `oldest_first` / `name`). The folder is re-checked after every file, so
  a fresh recording goes next even while a long backlog is being processed.
- **Multiple PCs can share one folder:** each PC claims a file before working
  on it (hidden `.inprogress` file in the output folder), so no file is done
  twice. Claims from a PC that crashed or was closed are taken over after
  10 minutes; the same PC restarting resumes its unfinished file at once.
- Startup shows a summary ("Found N audio files (X already transcribed,
  Y to do)"), the queue order and this PC's name.
- README: guide for large archives, long recordings and running on several
  PCs, with measured speeds.

### Changed
- Progress now shows how many files are still waiting instead of `[1/3]`.

## [1.0.0] - 2026-11-05

First release.

### Features
- Transcribes audio and video files (`.mp3`, `.mp4`, `.m4a`, `.wav`, `.flac`,
  `.ogg`, `.opus`, `.webm`, `.aac`, `.wma`, `.mkv`, `.mov`) to `.txt` using
  faster-whisper, fully offline after the first model download.
- Watches the input folder and transcribes new files automatically, waiting
  until each file has finished copying.
- Skips files that already have a transcript; never modifies the originals.
- Settings in `config.json`: model size, language, timestamps, silence filter,
  filename hint, file types, poll interval, device.
- Uses the file name as a hint to improve names and song titles, and removes
  lines Whisper is known to invent during music ("Thanks for watching!").
- Uses an NVIDIA GPU automatically when available, otherwise the CPU.
- Default folders are `Documents\mp3` and `Documents\transcribed` of the
  current user, created on first run.
- Only one copy can watch a folder at a time.
- Files with the same name but different extensions get separate transcripts.
- Log file capped at 1 MB with 3 backups.
- Distributed as a folder + zip built automatically by GitHub Actions.
