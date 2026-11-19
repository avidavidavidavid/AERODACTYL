@echo off
rem EXPERIMENTAL: drag STEP / STL / 3MF / G-code files onto this to make non-planar versions.
rem Use plain files, not _brick ones. Options: python tools\wavify.py --help
setlocal
set "PY=C:\Python315\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0tools\wavify.py" %*
echo.
pause
