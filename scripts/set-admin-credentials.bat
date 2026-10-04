@echo off
rem Set the Cognita admin username + password, then restart the server so the new
rem credentials take effect. Thin launcher — logic is in
rem scripts\set-admin-credentials.py (which writes only the password HASH).
rem
rem   scripts\set-admin-credentials.bat
rem
setlocal
set "REPO=%~dp0.."
set "PY=%REPO%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem 1. Prompt + write the new credentials (exits here on empty/mismatch).
"%PY%" "%REPO%\scripts\set-admin-credentials.py" %*
if errorlevel 1 exit /b %errorlevel%

rem 2. Restart Cognita so the admin app reloads the credentials (read at startup;
rem    there is deliberately NO live password-reset endpoint).
echo.
echo Restarting Cognita for the change to take effect...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'cognita serve' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
timeout /t 2 >nul
start "Cognita" /D "%REPO%" "%PY%" -m cognita serve
echo Done. Cognita restarted with the new admin credentials.
endlocal
