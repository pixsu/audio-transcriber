@echo off
REM ============================================================
REM  One-click build: creates a venv, installs the pinned requirements,
REM  and builds the program folder dist\AudioTranscriber\
REM  (plus dist\AudioTranscriber.zip for copying to other PCs).
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
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt --only-binary=av || goto :error

REM Build into a staging folder first. PyInstaller wipes its output folder,
REM and we don't want that to delete your models, config.json or log.
echo Building (this takes a few minutes)...
".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm --clean ^
    --onedir --console ^
    --name AudioTranscriber ^
    --distpath build\staging ^
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

REM Copy the program into dist\AudioTranscriber. Only the program files are
REM replaced; models\, config.json and transcriber.log are left alone.
echo Updating dist\AudioTranscriber ...
robocopy "build\staging\AudioTranscriber\_internal" "dist\AudioTranscriber\_internal" /MIR /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :error
copy /y "build\staging\AudioTranscriber\AudioTranscriber.exe" "dist\AudioTranscriber\" >nul || goto :error

REM Zip a clean copy (no models or personal settings) for other PCs.
REM (tar.exe is built into Windows 10/11; -a picks the format from ".zip")
echo Creating dist\AudioTranscriber.zip ...
if exist "dist\AudioTranscriber.zip" del "dist\AudioTranscriber.zip"
tar -a -c -f "dist\AudioTranscriber.zip" -C "build\staging" AudioTranscriber
if not exist "dist\AudioTranscriber.zip" goto :error

echo.
echo ============================================================
echo  Build finished:
echo    %~dp0dist\AudioTranscriber\AudioTranscriber.exe
echo  To use on another PC, copy dist\AudioTranscriber.zip, unzip it
echo  and double-click AudioTranscriber.exe inside the folder.
echo  config.json, the log and models\ are created next to the .exe.
echo ============================================================
pause
exit /b 0

:error
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
