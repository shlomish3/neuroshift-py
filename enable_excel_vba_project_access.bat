@echo off
setlocal

echo Enable Excel VBA project access
echo.
echo This lets Neuro Shift update the duplicate-name coloring macro in the
echo Excel template automatically.
echo.
echo Close Excel before continuing.
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$versions = @('16.0','15.0','14.0');" ^
  "foreach ($v in $versions) {" ^
  "  $path = 'HKCU:\Software\Microsoft\Office\' + $v + '\Excel\Security';" ^
  "  New-Item -Path $path -Force | Out-Null;" ^
  "  New-ItemProperty -Path $path -Name AccessVBOM -PropertyType DWord -Value 1 -Force | Out-Null;" ^
  "}" ^
  "Write-Host 'Excel VBA project access enabled for current Windows user.'"

echo.
echo Done. Now run run_neuroshift.bat again.
pause
