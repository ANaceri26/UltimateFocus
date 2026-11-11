@echo off
rem Builds the Windows apps into dist\
rem   FocusLock.exe      double-click app (window, no console)
rem   focuslock-cli.exe  commands, e.g. "focuslock-cli password set"
rem   focusdns.exe       optional Go DNS resolver (skipped if Go isn't installed)
rem Run from the project folder with the venv active:  scripts\build.bat
setlocal
cd /d "%~dp0.."

python -m pip install --upgrade pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
    --name FocusLock run_focuslock_gui.py || exit /b 1
python -m PyInstaller --noconfirm --onefile --console --uac-admin ^
    --name focuslock-cli run_focuslock.py || exit /b 1

where go >nul 2>nul
if errorlevel 1 (
    echo Go not found: skipping focusdns.exe
) else (
    pushd dns
    set GOOS=windows
    set GOARCH=amd64
    go build -trimpath -ldflags "-s -w" -o ..\dist\focusdns.exe .\cmd\focusdns || (popd & exit /b 1)
    popd
)

echo.
echo Built:
dir /b dist\*.exe
