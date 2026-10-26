@echo off
rem One command: the simulator server and web UI, then the browser (scripts/host.py). Options as for host.py.
cd /d "%~dp0"
uv run python scripts\host.py %*
if errorlevel 1 pause
