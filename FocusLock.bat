@echo off
rem Double-click to open FocusLock when running from source (needs Python 3.11+).
rem Uses the project's .venv if it exists, otherwise the Python on PATH.
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -m focuslock gui
if errorlevel 1 pause
