@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if not errorlevel 1 (
  uv run --offline launch.py || pause
  exit /b
)
if exist "%USERPROFILE%\.local\bin\uv.exe" (
  "%USERPROFILE%\.local\bin\uv.exe" run --offline launch.py || pause
  exit /b
)
echo Install uv and run uv sync once, then reopen Switchboard.
pause
