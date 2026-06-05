@echo off
setlocal

cd /d "%~dp0"

echo Neuro Shift roster export
echo.
echo Updating Excel macro template...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update_template_vba.ps1"
if not %errorlevel%==0 (
  echo Warning: could not update the Excel VBA template automatically.
  echo The export will continue, but duplicate-name coloring may use the old macro.
  echo.
)

set /p YEAR=Enter year, for example 2026: 
set /p MONTH=Enter month number, for example 6 or 06: 

if "%YEAR%"=="" (
  echo Year is required.
  pause
  exit /b 1
)

if "%MONTH%"=="" (
  echo Month is required.
  pause
  exit /b 1
)

if "%MONTH:~1%"=="" set "MONTH=0%MONTH%"
set "TARGET_MONTH=%YEAR%-%MONTH%"

echo.
echo Creating roster for %TARGET_MONTH%...
echo.

set "CONDA_BAT=%USERPROFILE%\miniconda3\Library\bin\conda.bat"
if not exist "%CONDA_BAT%" (
  echo Could not find Conda at:
  echo %CONDA_BAT%
  echo.
  echo Please update CONDA_BAT in this file or run from an activated neuroshift environment.
  pause
  exit /b 1
)

call "%CONDA_BAT%" activate neuroshift
if not %errorlevel%==0 (
  echo Could not activate the neuroshift Conda environment.
  pause
  exit /b 1
)

python -m core.assign2 "%TARGET_MONTH%"
set "RUN_EXIT=%errorlevel%"

echo.
if "%RUN_EXIT%"=="0" (
  echo Done. Check the output_roster folder.
) else (
  echo The export failed. Review the message above.
)
pause
exit /b %RUN_EXIT%
