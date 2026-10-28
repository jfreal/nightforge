@echo off
REM PR dashboard. Double-click, or run from a terminal. Close this window to stop it.
setlocal
set PYTHONIOENCODING=utf-8
python "%~dp0server.py" --config "%~dp0config.json" %*
endlocal
