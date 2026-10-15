@echo off
rem Abre o Torreta. Na primeira vez cria o ambiente virtual e instala as dependencias.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Preparando o ambiente pela primeira vez...
  python -m venv .venv || exit /b 1
  ".venv\Scripts\python" -m pip install --quiet --disable-pip-version-check -r requirements.txt || exit /b 1
)
".venv\Scripts\python" -m torreta %*