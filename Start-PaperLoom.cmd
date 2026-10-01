@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_gui.ps1" %*
set "PAPERLOOM_GUI_EXIT=%errorlevel%"
if not "%PAPERLOOM_GUI_EXIT%"=="0" (
    echo PaperLoom could not start. See the message above.
    echo For a new installation, run Setup-PaperLoom.cmd first.
    pause
)
exit /b %PAPERLOOM_GUI_EXIT%
