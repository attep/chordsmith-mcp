@echo off
rem Start the ChordSmith VST bridge (Windows-only: it drives the Windows plugins for a server).
rem Requires CHORDSMITH_VST_BRIDGE_TOKEN, the same value the server uses.
rem Extra arguments are passed through, e.g.:
rem   start-bridge.cmd --musescore "C:\Program Files\MuseScore 4\bin\MuseScore4.exe"
setlocal
if "%CHORDSMITH_VST_BRIDGE_TOKEN%"=="" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.env") do (
    if /i "%%A"=="CHORDSMITH_VST_BRIDGE_TOKEN" set "CHORDSMITH_VST_BRIDGE_TOKEN=%%B"
  )
)
if "%CHORDSMITH_VST_BRIDGE_TOKEN%"=="" (
  echo Set CHORDSMITH_VST_BRIDGE_TOKEN first ^(the same value the server uses^, e.g. in .env^).
  exit /b 1
)
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0vst_bridge.py" %*
