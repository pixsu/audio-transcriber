@echo off
REM ============================================================
REM  One-click build: creates a venv, installs requirements,
REM  and builds dist\MP3Transcriber.exe with PyInstaller.
REM ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install 64-bit Python 3.11 or 3.12 from
    echo https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
)

echo Installing requirements...
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt --only-binary=av || goto :error

echo Building the .exe (this takes a few minutes)...
".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm --clean ^
    --onefile --console ^
    --name MP3Transcriber ^
    --collect-all faster_whisper ^
    --collect-all ctranslate2 ^
    --collect-all onnxruntime ^
    --collect-all av ^
    --collect-all tokenizers ^
    --copy-metadata faster-whisper ^
    --copy-metadata huggingface_hub ^
    --copy-metadata tokenizers ^
    --copy-metadata tqdm ^
    --copy-metadata numpy ^
    transcriber.py || goto :error

echo.
echo ============================================================
echo  Build finished: %~dp0dist\MP3Transcriber.exe
echo  Copy it anywhere and double-click it. config.json, the
echo  log and the models folder are created next to the .exe.
echo ============================================================
pause
exit /b 0

:error
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
