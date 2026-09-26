@echo off
setlocal
cd /d "%~dp0"
python -B migrate_convex.py %*
set "taskExitCode=%errorlevel%"
pause
exit /b %taskExitCode%
