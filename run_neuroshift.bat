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

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m core.assign2 "%TARGET_MONTH%"
) else (
  python -m core.assign2 "%TARGET_MONTH%"
)

echo.
if %errorlevel%==0 (
  echo Done. Check the output_roster folder.
) else (
  echo The export failed. Review the message above.
)
pause
