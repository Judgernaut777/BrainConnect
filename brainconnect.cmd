@echo off
REM Convenience wrapper so `brainconnect ...` works from the repo root on Windows.
REM Prefers the repo venv's console script; falls back to one on PATH.
setlocal
set "VENVBC=%~dp0.venv\Scripts\brainconnect.exe"
if exist "%VENVBC%" (
  "%VENVBC%" %*
) else (
  brainconnect %*
)
exit /b %ERRORLEVEL%
