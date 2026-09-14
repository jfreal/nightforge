@echo off
REM Copy the sweep scripts to the folder the scheduled task runs.
REM Preview: deploy.cmd    Write: deploy.cmd --apply
setlocal
set PYTHONIOENCODING=utf-8
python "%~dp0deploy.py" --config "%~dp0config.json" %*
endlocal
