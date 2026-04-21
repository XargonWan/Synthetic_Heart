@echo off
echo Stopping SyntH...
for /f "tokens=2 delims=," %%p in ('wmic process where "name='python.exe' and commandline like '%%main.py%%'" get processid /format:csv 2^>nul ^| findstr /v "ProcessId"') do (
    if not "%%p"=="" taskkill /pid %%p /f >nul 2>&1
)
echo Done.
