@echo off
title Pad
cd /d "%~dp0"
if exist "dist\code.txt" (
  echo Code from dist\code.txt:
  type "dist\code.txt"
  echo.
)
if exist "dist\PadServer.exe" (
  "dist\PadServer.exe"
) else (
  py server.py
)
pause
