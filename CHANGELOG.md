# Changelog

All notable changes to this project are listed here.
Versions follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH` (breaking change / new feature / bug fix).

## [Unreleased]

## [1.25.0] - 2026-11-11

### Changed
- **Find repeats covers your whole history, not just the Inbox.** Every
  recording that's transcribed now leaves a small sound fingerprint (about
  350 KB per hour) in a hidden `.fingerprints` folder in the Transcripts
  folder. The finder compares the newest 200 of them in about a second,
  without reading any audio, and still finds repeats after the originals
  have been moved or deleted. Older recordings still in the Inbox are
  fingerprinted the first time you open Find repeats.
- **It checks by itself.** Every 30 minutes the program looks for new
  repeats in the background; the button shows how many are waiting
  ("Find repeats (4)") and a notification pops up when new ones appear.
- Clips whose original recordings are all gone are still listed (and are
  written into those transcripts when saved from another airing), but
  can't be played or saved on their own.
- Long stretches that always air together (e.g. a nightly sign-off
  sequence) are shown as one item instead of many overlapping pieces, and
  very common jingles (airing in 100+ recordings) are now found too.

## [1.24.1] - 2026-11-11

### Fixed
- Save window: the transcript lines around the clip are now a short fixed
  preview (2 lines before, the clip — long ones shortened — and 2 after)
  instead of a small scrolling box that redrew badly when scrolled.

## [1.24.0] - 2026-11-11

### Fixed
- **Exact start and end for repeated clips.** Before Play or Save, Find
  repeats lines up the airings 0.1 s by 0.1 s and trims to where they really
  match, so Play plays the actual jingle and the times written into the
  transcripts are right for every airing (not rounded to whole seconds).
- **Open full transcript always appears in front** and responds. Before, it
  could open behind the save window, which kept it locked.
- **Jingles, ads and songs list:** buttons are no longer squeezed by long
  text, and the text wraps.

### Added
- The save window shows the transcript lines around the clip, with the clip
  highlighted, so you can see what plays before and after it.
- Saved items have **▶ Play** (a 30-second preview stored with each one)
  and **Edit words** in the Jingles, ads and songs list.
- Play starts quickly: only the clip is read, not the whole recording.

## [1.23.0] - 2026-11-11

### Added
- **▶ Play** in Find repeats and in the save window: hear the clip before
  saving it (click again to stop).
- The save window says where the clip was heard ("Heard in … at 00:00:12
  (and 3 more)"), and **Open transcript** opens that transcript scrolled
  to the moment, with the clip's lines highlighted.

## [1.22.0] - 2026-11-11

### Changed
- **Saving a jingle, ad or song asks only for the correct words.** The
  times, name and type are filled in automatically (name from the first
  words, type from the length) and tucked under **More options**.
- **Find repeats** lists each clip as "Jingle · 38 s · aired 4 times" with
  its words underneath, and saving writes it straight into the transcripts
  where it aired (no extra question). Saving from a transcript's selected
  lines replaces those lines straight away too.

## [1.21.0] - 2026-11-11

### Added
- **Find repeats** (Settings → Vocabulary → Jingles, ads, songs → **Find
  repeats…**): compares your newest recordings in the Inbox (up to 30) and
  lists audio that aired more than once — jingles, station IDs, ads, songs —
  with how long it is, how often it aired and its transcript text. **Save…**
  opens the save window with the times and text filled in; type the correct
  text once and it's used in every new file, and can be written straight
  into the transcripts where it aired. About 5 seconds per hour of audio.
- **Warnings on risky fixes.** Before a fix is saved (Vocabulary window, or
  right-click → Correct → Always), the program checks it: if the word is
  already common in your transcripts or very short, it may be a real word
  that the fix would also change; and if one fix undoes another
  (`A = B` and `B = C`), it says so. You can still save it anyway, or keep
  a correction to just that transcript.

## [1.20.0] - 2026-11-11

### Changed
- **Simpler settings.** The Mode (Fast / Accurate) switch is gone: the
  program always uses Accurate, which gives better transcripts on music,
  noise and accents (about 30% slower than Fast). PCs that were on Fast are
  moved to Accurate once; `"speed": "fast"` can still be set by hand in
  config.json for a very slow PC.
- **Skip silence is hidden while Label music and songs is on**, because it
  has no effect then (only real dead air is skipped).
- **New installs start with Add timestamps and Label music and songs on.**
- First-run setup: **Most accurate** now means Turbo (1.6 GB, recommended,
  pre-selected) instead of the much slower Large model.

## [1.19.2] - 2026-11-11

### Changed
- **Updates are found sooner.** The program checks for a new version every
  hour (instead of once a day), so you no longer need **Menu → Check for
  updates**. A Windows notification pops up once per new version, so you
  see it even when the program is in the tray.

## [1.19.1] - 2026-11-11

### Fixed
- Vocabulary window: the two lists are equal width, the hints and the note
  at the bottom no longer get cut off or covered by the buttons, and long
  fixes stay on one line.

## [1.19.0] - 2026-11-11

### Changed
- **Names and fixes are shared between PCs.** They're now kept in a hidden
  `.vocabulary.json` in the Transcripts folder (next to the jingle
  library), so every PC using that folder shares one list: a fix added on
  one PC is used by all of them from their next file. Each PC's existing
  lists are moved there automatically the first time this version runs
  (merged, nothing lost). If the folder can't be reached, a PC keeps using
  its own lists until it can.

## [1.18.1] - 2026-11-11

### Changed
- Stretches where music plays but no words can be made out are labelled
  `(music, no clear words)` instead of `(instrumental)`: they are often a
  song's musical break or wordless "la la la" singing, not music without a
  song.

## [1.18.0] - 2026-11-11

### Changed
- **No gaps or jumps in the timeline** (with Label music and songs on):
  every line starts exactly where the one before ended, from 00:00:00 to
  the end of the file. Pauses of 3 seconds or more get their own line
  (`[SILENCE …]`, `[MUSIC …] (instrumental)`, `(unclear speech)`); shorter
  pauses are joined onto the line before.
- **Save as jingle / ad / song** saves the text as one normal line again
  (no line-by-line lyrics).

## [1.17.0] - 2026-11-10

### Added
- **Songs and ads in the library, line by line.** **Save as jingle / ad /
  song** now has a type (Jingle, Ad, Song) and keeps each line's time: fix
  the words after each `[time]` and every line is written with its own
  timestamp whenever that recording airs again, e.g.
  `[SONG 00:01:57 → 00:01:59] …`. Useful for songs the station plays often
  and for languages Whisper doesn't know (such as Bikol).
- Matching works in 8-second pieces, so a song that's cut short, faded
  early or talked over is still recognised on its clean parts (only the
  lines in the matched part are used).

### Fixed
- Whisper repeating a phrase dozens of times ("pag-uswag na pag-uswag na
  …") is now shortened to three times plus "…" instead of filling the line.
- "Thank you so much." invented over music is dropped.

## [1.16.0] - 2026-11-10

### Added
- **Jingle library.** Jingles, station IDs and ads are the same recording
  every time they air. Correct one once (transcript window: select its
  lines, right-click → **Save as jingle / ad**, type the correct text) and
  it's recognised by its sound in every new file and written exactly as
  saved: `[JINGLE 00:00:45 → 00:01:23] Tuloy ang ligaya! …`. Recognised
  stretches aren't transcribed, which also saves time. Shared by PCs using
  the same Transcripts folder; see and delete them in **Settings →
  Vocabulary → Jingles and ads**.

## [1.15.0] - 2026-11-10

### Added
- **Nothing is left blank** (with Label music and songs on): stretches
  with sound but no words get their own timestamped line, e.g.
  `[MUSIC 00:00:22 → 00:00:45] (instrumental)` or `(unclear speech)`.

### Fixed
- **Same result every time.** Whisper retries hard parts with some
  randomness, so re-transcribing a file could give different (sometimes
  worse) results. It now starts from a fixed point and a file always comes
  out the same.
- The second pass over skipped stretches is more reliable: it no longer
  uses random retries, and tries each half of a stretch if the whole one
  gives nothing.
- Caption words Whisper invents ("Outro", "Intro Music") are dropped.

## [1.14.0] - 2026-11-10

### Fixed
- **More accurate timestamps.** Times now come from the words themselves,
  so a line no longer "starts" at the beginning of a song intro or runs on
  for 20 seconds after the last word.
- **Skipped sung or chanted parts are recovered** (with Label music and
  songs on). Whisper sometimes jumped over a jingle to the next spoken line;
  stretches with sound but no text are now transcribed a second time on
  their own. Only confident lines are kept, and filler Whisper invents over
  instrumentals ("Thank you.", "Outro Music") is dropped. Adds about 25% to
  the time of music-heavy files.
- Lines that are one short bit repeated over and over
  ("Pag-ag-ag-ag-ag...") are dropped.

## [1.13.0] - 2026-11-10

### Added
- **Edit transcripts in the app.** The transcript window has an **Edit**
  button: rewrite sentences, delete lines or press Enter to split a line,
  then **Save** (Ctrl+S) or **Cancel**. Closing with unsaved changes asks
  first. Right-click → Correct still works for word fixes when not editing.

## [1.12.0] - 2026-11-10

### Added
- **Correct words while reading.** **Open** in the Completed tab now shows
  the transcript inside the app. Select a wrong word (or double-click it),
  right-click → **Correct**, type the right spelling and choose **Always**:
  the transcript is fixed, the fix is added to Vocabulary for future
  transcripts, and older transcripts with the same mistake can be fixed too.
  **Open in Notepad** is still there.

### Changed
- Fixes follow the transcript's capitals when written in normal capitals
  (`Tuyoy = Tuloy` turns "tuyoy" into "tuloy"). Names with special capitals
  (`Love rate = Love Radio`) are still written exactly as typed.

## [1.11.0] - 2026-11-10

### Added
- **Vocabulary** (Settings → Vocabulary → Edit): a list of names to spell
  right (stations, hosts, sponsors, places), given to Whisper as hints, and
  a list of fixes (`Love rate = Love Radio`) applied to every line.
- **Turbo** quality (`large-v3-turbo`, 1.6 GB): more accurate than Medium on
  radio recordings (names, ads and lyrics) for about 30% more time.

### Fixed
- Stray Korean, Chinese or Russian letters that Whisper sometimes invents
  during music are removed when the language is Filipino/Tagalog or English.

## [1.10.4] - 2026-11-10

### Fixed
- With **Label music and songs** on, Whisper no longer randomly drops whole
  30-second stretches it thinks have "no speech" (e.g. chanted jingles).
  Real silence is still skipped by the dead-air check.

## [1.10.3] - 2026-11-10

### Fixed
- **Missing speech in radio recordings.** Two causes, both fixed:
  - Logger file names like `2026-04-26 00-00-00 DWCM - FM (Backup).mp3`
    were given to Whisper as a meaningless hint, which could blank out the
    start of the file. Numbers and words like "Backup" or "FM" are now
    ignored, and no hint is used unless at least two real words are left.
  - After a repeated lyric, Whisper could get stuck and skip minutes of
    clear speech. Each 30-second chunk is now transcribed without the
    previous chunk's text. On a test recording this recovered the intro and
    two minutes of prayer and station sign-off (41 → 109 lines).

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
