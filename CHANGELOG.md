# Changelog

All notable changes to this project are listed here.
Versions follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH` (breaking change / new feature / bug fix).

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
