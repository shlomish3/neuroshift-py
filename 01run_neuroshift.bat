@echo off
setlocal

cd /d "%~dp0"

set "ENV_DIR=%USERPROFILE%\miniconda3\envs\neuroshift"
set "PYTHONW=%ENV_DIR%\pythonw.exe"
set "PYTHON=%ENV_DIR%\python.exe"

if exist "%PYTHONW%" (
  start "" "%PYTHONW%" -m gui.app
  exit /b 0
)

if exist "%PYTHON%" (
  start "" "%PYTHON%" -m gui.app
  exit /b 0
)

echo Could not find the neuroshift Python environment at:
echo %ENV_DIR%
echo.
echo Expected either pythonw.exe or python.exe.
echo.
echo Please ask Codex to repair the neuroshift environment.
pause
exit /b 1
