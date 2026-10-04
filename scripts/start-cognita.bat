@echo off
rem Start the Cognita gateway + admin UI (console mode; Ctrl+C to stop).
rem The window stays open after exit so crashes / port conflicts (e.g. a
rem second instance losing the bind on 8675) remain readable.
cd /d "%~dp0.."
venv\Scripts\python.exe -m cognita serve
echo.
echo Cognita exited with code %ERRORLEVEL%.
pause
