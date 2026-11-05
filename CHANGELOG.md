# Changelog

All notable changes to this project are listed here.
Versions follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH` (breaking change / new feature / bug fix).

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
