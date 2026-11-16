@echo off
setlocal
cd /d "%~dp0"

python -c "import sys; sys.exit(sys.version_info[0] != 3)" >nul 2>nul
if not errorlevel 1 (
  python servidor.py
  goto :fin
)

py -3 -c "import sys; sys.exit(sys.version_info[0] != 3)" >nul 2>nul
if not errorlevel 1 (
  py -3 servidor.py
  goto :fin
)

echo No se encontro Python en PATH.
echo Instala Python 3 desde https://www.python.org/downloads/windows/
echo y marca la opcion "Add python.exe to PATH" durante la instalacion.
pause

:fin
endlocal
