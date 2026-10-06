@echo off
rem EXIF Info Tool launcher (web UI, merged)
set "PY=C:\Users\stfizer\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\stfizer\.workbuddy\binaries\python\versions\3.13.12\python.exe"
"%PY%" "%~dp0exif_tool.py"
pause
