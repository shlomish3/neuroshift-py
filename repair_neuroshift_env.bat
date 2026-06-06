@echo off
setlocal

cd /d "%~dp0"

echo Neuro Shift environment repair
echo.
echo This replaces pip-installed numpy/pandas with Conda packages.
echo Close Excel and any Python error popups before continuing.
echo.
pause

set "CONDA_BAT=%USERPROFILE%\miniconda3\Library\bin\conda.bat"
if not exist "%CONDA_BAT%" (
  echo Could not find Conda at:
  echo %CONDA_BAT%
  pause
  exit /b 1
)

call "%CONDA_BAT%" activate neuroshift
if not %errorlevel%==0 (
  echo Could not activate the neuroshift Conda environment.
  pause
  exit /b 1
)

python -m pip uninstall -y numpy pandas
if not %errorlevel%==0 (
  echo.
  echo Could not remove the pip packages. Reboot Windows, then run this repair again.
  pause
  exit /b 1
)

call "%CONDA_BAT%" install -n neuroshift -c conda-forge "numpy<2.3" "pandas<2.3" openpyxl -y
if not %errorlevel%==0 (
  echo.
  echo Conda package installation failed. Review the message above.
  pause
  exit /b 1
)

python -c "import pandas, openpyxl; print('Environment repair OK')"
if not %errorlevel%==0 (
  echo.
  echo Packages installed, but import check still failed.
  pause
  exit /b 1
)

echo.
echo Done. You can run run_neuroshift.bat again.
pause
