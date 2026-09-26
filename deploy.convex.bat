@echo off
cd /d "%~dp0"
python -B -m anchorbot.launcher deploy
set "taskExitCode=%errorlevel%"
pause
exit /b %taskExitCode%
