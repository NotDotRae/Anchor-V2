@echo off
start "AnchorBot" /D "%~dp0" cmd /k "python -B -m anchorbot.launcher run %*"
