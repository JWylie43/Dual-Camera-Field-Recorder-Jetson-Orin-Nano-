@echo off
REM stitch.bat - double-click launcher for the panorama tuner on Windows.
REM
REM Builds the binary on first run, then launches it. The program locates the
REM calibration folder relative to its own .exe, so it works no matter which
REM directory it's started from (including a double-click).
REM
REM Requires (one-time): Visual Studio 2022 (C++ workload), CMake, OpenCV, and
REM optionally FFmpeg on PATH. See the README "Prerequisites" section.

cd /d "%~dp0"

if not exist "build\Release\StitchPipeline.exe" (
  echo Building StitchPipeline ^(first run; this may take a minute^)...
  cmake -S . -B build
  cmake --build build --config Release
)

if not exist "build\Release\StitchPipeline.exe" (
  echo.
  echo Build did not produce build\Release\StitchPipeline.exe - see messages above.
  echo A common cause is antivirus blocking the linker; add an exception for this folder.
  pause
  goto :eof
)

"build\Release\StitchPipeline.exe"

echo.
echo Program exited. Press any key to close this window.
pause >nul
