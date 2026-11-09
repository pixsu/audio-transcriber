# Changelog

All notable changes to this project are listed here.
Versions follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH` (breaking change / new feature / bug fix).

## [Unreleased]

## [1.10.2] - 2026-11-09

### Fixed
- The update notice no longer squeezes the **Update now** button in a
  narrow window. Its text is now two short lines (e.g. "Version 1.10.2 is
  available" / "You have 1.10.1"), also while downloading and installing.

## [1.10.1] - 2026-11-09

### Fixed
- **Skip this file**, **Pause** and quitting now respond within a few
  seconds, even during long silence or music without words. Before, they
  waited until Whisper produced the next line of text, which in a file with
  a long silent stretch could take many minutes.

## [1.10.0] - 2026-11-09

### Added
- **Label music and songs** (Settings → Options, off by default): songs,
  jingles and other music are still transcribed, with `MUSIC` in front of
  their lines, e.g. `[MUSIC 00:01:23 → 00:01:30] lyrics`. Talk over a music
  bed stays unlabelled. Uses a small offline sound classifier (YAMNet,
  14 MB, downloaded once and checked against a fixed fingerprint) and adds
  only a few seconds per hour of audio.
- **Dead air is skipped and marked** when music labels are on: stretches
  with no sound for 10 seconds or more are not transcribed and appear as
  `[SILENCE 00:52:40 → 01:00:30]`. A mostly silent hour finishes in minutes,
  and Whisper can't invent text in the silence.

### Changed
- With music labels on, **Skip silence** is not used, because it drops most
  singing.

### Known limits
- Lyrics, especially sung Tagalog, are less accurate than speech.
- Very quiet or muffled music (e.g. a low-quality anthem recording) may not
  be labelled.

## [1.9.0] - 2026-11-09

### Added
- **Speed in the Completed tab:** each transcript shows how long it took
  next to the audio length, e.g. "1 h 05 min audio → 28 min". Times are kept
  in the Transcripts folder (one small hidden file per PC), so they survive
  restarts and are shared between PCs.
- **Problem reports:** **Send report** in the Issues tab (and
  **Menu → Create problem report…**) saves a zip on the Desktop with the
  log, settings and basic PC details, ready to email. No audio or
  transcripts are included.

### Changed
- Installers left over from one-click updates are deleted automatically.

## [1.8.0] - 2026-11-09

### Added
- **One-click updates:** the update notice now has **Update now**. It
  downloads the new installer in the background (with progress), checks its
  SHA-256 fingerprint, installs it silently and reopens the program on the
  new version, with settings and models kept. It never interrupts a file:
  the update waits until the current file is done, or click **Install now**.
- Each release now includes the installer's `.sha256` fingerprint.

### Note
- Updating from 1.7.0 or older still works the old way one last time.

## [1.7.0] - 2026-11-09

### Added
- **First-run setup:** on first launch a short 3-step window asks where to
  put audio files (Inbox), where transcripts go, and how accurate they should
  be: Fast, Balanced (recommended) or Most accurate, with no model names.
- **Tray tooltip with progress,** e.g. "Transcribing 3 of 12 · 45%".

### Fixed
- The search box in the Completed tab now shows its grey hint text.

## [1.6.0] - 2026-11-06

### Added
- **Installer** (`AudioTranscriber-Setup-<version>.exe`): Start Menu entry,
  optional desktop shortcut and "start when I log in". No administrator
  rights needed. Installing a newer version closes the running program first
  and keeps your settings and downloaded models. Uninstall from Windows
  Settings.
- **Update notice:** once a day the program checks GitHub for a newer
  release and shows a notice with Download, What's new and Later. Also
  **Menu → Check for updates**, and a **Check for updates** switch in
  Settings.
- **Search in the Completed tab:** find transcripts by file name or by words
  inside them (the matching sentence is shown), plus a date filter (Today,
  Last 7 days, Last 30 days). Searches every transcript, not just the newest
  100.

## [1.5.0] - 2026-11-06

### Added
- **Queue tab:** every waiting file in order, with size and when it arrived.
  **Do next** moves a file to the front; **Skip** takes it out of the queue.
  New files appear within seconds, even while a long file is running.
- **Completed tab:** finished transcripts with **Open** and **Show in folder**,
  and how long each took.
- **Issues tab:** failed and skipped files with the reason, plus **Retry** and
  **Retry all** (no restart needed any more).
- **Top bar** with Add files, Open Inbox, Open Transcripts and a **Menu**
  (Open log file, Edit config.json, Retry all, About, Quit).
- **Device** setting (Automatic / CPU only / NVIDIA GPU) and **Reset to
  defaults** in the Settings panel.
- **Status bar** showing the loaded model, this PC's name and file copies.
- Clear error state: when something is wrong (e.g. a folder can't be reached)
  the window says what to fix and offers **Try again**.
- Keyboard shortcuts: Ctrl+O (add files), Ctrl+Q (quit).
- **File details:** format, duration and size for queued and completed files;
  bitrate, mono/stereo and sample rate for the file being transcribed; word
  count for transcripts; total audio waiting in the status line. Details are
  read in the background, so large folders stay responsive.
- New dropdowns that match the window's theme (the old ones opened a plain
  Windows menu with a white border).
- **Apply / Discard** for settings: changes are no longer saved the moment
  you click them. The panel shows how many changes are waiting.
- Confirmation for bigger changes (model, device, folders, Start with
  Windows), explaining the effect, e.g. the download size of a new model.
- Quitting with unapplied settings asks whether to apply them.

### Changed
- Redesigned window: status card on top, tabs below, scrollable settings
  panel on the right; activity log moved to its own tab with coloured
  warnings and errors.
- Quitting while a file is being transcribed now asks first.
- Starting the program again while it is already running brings the
  existing window to the front instead of showing an error.
- Adding files checks the file type and won't overwrite a different file
  with the same name already in the Inbox.
- Choosing the same folder for Inbox and Transcripts is refused.
- Sharper text on high-resolution screens.

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
