@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_environment.ps1" %*
set "PAPERLOOM_SETUP_EXIT=%errorlevel%"
if not "%PAPERLOOM_SETUP_EXIT%"=="0" (
    echo Installation failed. Read the message above before retrying.
) else (
    echo Installation complete. Open Start-PaperLoom.cmd to start PaperLoom.
)
pause
exit /b %PAPERLOOM_SETUP_EXIT%
