@echo off
rem Drag STEP / STL / 3MF / G-code files onto this to make BrickLayers versions.
rem Extra options (printer, filament, walls...): run  python tools\brickify.py --help
setlocal
set "PY=C:\Python315\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0tools\brickify.py" %*
echo.
pause
