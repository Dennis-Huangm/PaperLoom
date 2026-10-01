@echo off
setlocal
cd /d "%~dp0.."
set "ARXIV_PDF_BACKEND=curl-direct"
if not exist ".venv\Scripts\python.exe" (
    echo Project Python not found. Run scripts\setup_environment.ps1 first.
    pause
    exit /b 1
)
echo Starting PaperLoom with direct curl PDF downloads.
echo API and LLM requests keep their existing proxy settings.
".venv\Scripts\python.exe" -m arxiv_ra --config config.yaml gui
exit /b %errorlevel%
