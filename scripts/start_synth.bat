@echo off
cd /d "%~dp0.."
echo Starting SyntH...
uv run python main.py
pause
