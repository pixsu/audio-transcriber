# Security policy

## Supported versions

Only the [latest release](https://github.com/pixsu/audio-transcriber/releases/latest)
receives fixes. The program tells you when a newer version is available.

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Report them
privately through
[GitHub's private vulnerability reporting](https://github.com/pixsu/audio-transcriber/security/advisories/new)
with steps to reproduce. You can expect a first reply within a week.

## What the program does with your data

- Audio files and transcripts never leave your PC (or your shared network
  folder). Transcription runs locally.
- Internet is used only to download the speech model the first time a model
  is used (from Hugging Face), and for the daily update check, which reads
  public release information from GitHub and sends nothing about you or your
  files. The update check can be turned off in Settings.
- Original audio files are only read, never modified, moved or deleted.

## Release integrity

Releases are built from this repository's source by GitHub Actions
([build.yml](.github/workflows/build.yml)); you can check which commit each
release was built from on the Actions tab. The program is not code-signed,
so Windows SmartScreen may warn the first time you run it.
