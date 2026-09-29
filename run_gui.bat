@echo off
title Report-to-JSON
cd /d "%~dp0"

rem Use the interpreter in %PYTHON% if set, else a local .venv\ or venv\, else python on PATH.
if not defined PYTHON (
    if exist ".venv\Scripts\python.exe" (
        set "PYTHON=.venv\Scripts\python.exe"
    ) else if exist "venv\Scripts\python.exe" (
        set "PYTHON=venv\Scripts\python.exe"
    ) else (
        set "PYTHON=python"
    )
)

"%PYTHON%" -u -m report_to_json
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Application exited with code %ERRORLEVEL%.
    pause
)
