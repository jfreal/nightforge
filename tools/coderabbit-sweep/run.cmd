@echo off
REM On-demand CodeRabbit sweep. Double-click, or run from a terminal.
setlocal
set PYTHONIOENCODING=utf-8
python "%~dp0sweep.py" --config "%~dp0config.json" %*
endlocal
