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

set "NUMPY_LIST=%TEMP%\neuroshift_numpy_list.txt"
set "PANDAS_LIST=%TEMP%\neuroshift_pandas_list.txt"

call "%CONDA_BAT%" list -n neuroshift numpy > "%NUMPY_LIST%" 2>nul
findstr /R /C:"^numpy .*pypi" "%NUMPY_LIST%" >nul
if %errorlevel%==0 goto broken_python_env

call "%CONDA_BAT%" list -n neuroshift pandas > "%PANDAS_LIST%" 2>nul
findstr /R /C:"^pandas .*pypi" "%PANDAS_LIST%" >nul
if %errorlevel%==0 goto broken_python_env

del "%NUMPY_LIST%" "%PANDAS_LIST%" >nul 2>nul

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

:broken_python_env
del "%NUMPY_LIST%" "%PANDAS_LIST%" >nul 2>nul
echo The neuroshift Conda environment has pip-installed numpy/pandas,
echo which is currently crashing before the roster can run.
echo.
echo Please close any Python error popups, then run:
echo   repair_neuroshift_env.bat
echo.
echo After repair finishes, run this file again.
pause
exit /b 1
