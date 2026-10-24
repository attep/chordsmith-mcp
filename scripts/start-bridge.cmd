@echo off
rem Start the ChordSmith VST bridge (Windows-only: it drives the Windows plugins for a server).
rem Reads the bridge token and the vst_host path from .env (or the environment); falls back to
rem this checkout's bin\vst_host.exe. Extra arguments are passed through, e.g.:
rem   start-bridge.cmd --musescore "C:\Program Files\MuseScore 4\bin\MuseScore4.exe"
setlocal

if "%CHORDSMITH_VST_BRIDGE_TOKEN%"=="" if exist "%~dp0..\.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.env") do (
    if /i "%%A"=="CHORDSMITH_VST_BRIDGE_TOKEN" set "CHORDSMITH_VST_BRIDGE_TOKEN=%%B"
  )
)
if "%CHORDSMITH_VST_BRIDGE_TOKEN%"=="" (
  echo Set CHORDSMITH_VST_BRIDGE_TOKEN first ^(the same value the server uses^, e.g. in .env^).
  exit /b 1
)

rem vst_host: environment, then .env, then this checkout's bin folder.
if "%CHORDSMITH_VST_HOST%"=="" if exist "%~dp0..\.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.env") do (
    if /i "%%A"=="CHORDSMITH_VST_HOST" set "CHORDSMITH_VST_HOST=%%B"
  )
)
if "%CHORDSMITH_VST_HOST%"=="" for %%I in ("%~dp0..\bin\vst_host.exe") do set "CHORDSMITH_VST_HOST=%%~fI"
if not exist "%CHORDSMITH_VST_HOST%" (
  echo vst_host not found: "%CHORDSMITH_VST_HOST%"
  echo Set CHORDSMITH_VST_HOST to the vst_host.exe path.
  exit /b 1
)

rem Tab import (import_tab) needs MuseScore; use the usual install when none was given.
if "%CHORDSMITH_MUSESCORE%"=="" if exist "C:\Program Files\MuseScore 4\bin\MuseScore4.exe" set "CHORDSMITH_MUSESCORE=C:\Program Files\MuseScore 4\bin\MuseScore4.exe"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0vst_bridge.py" %*
