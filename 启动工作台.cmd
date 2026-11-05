@echo off
rem ASCII-only on purpose: cmd.exe parses .cmd with the OEM code page.
rem All Chinese messages are printed by the Python workbench, not here.
rem Entry: finds a Python 3.9+ (runtime\ -> .workbuddy -> PATH); if none
rem exists, offers to auto-download a portable Python + pip (needs network).
setlocal
cd /d "%~dp0"

rem ---- 1) look for an existing Python ----
set "PYEXE="
if defined XIANYU_PYTHON set "PYEXE=%XIANYU_PYTHON%"
if not defined PYEXE if exist "runtime\python.exe" set "PYEXE=runtime\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if defined PYEXE goto :havepy

rem ---- 2) none found: offer one-time auto-download of portable Python ----
echo [SETUP] No Python found on this computer.
choice /c YN /n /m "Download portable Python + pip automatically now (about 12 MB, internet needed)? [Y/N] "
if errorlevel 2 goto :nopy
call :bootstrap
if errorlevel 1 goto :bootfail
set "PYEXE=%CD%\runtime\python.exe"
echo [SETUP] Python is ready. After the workbench opens, use page 1
echo         "Install dependencies" once (downloads the Python packages).
echo.

:havepy
"%PYEXE%" -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 goto :badpy

rem ---- 3) free port 8765 if an old workbench is still holding it ----
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /R /C:":8765 " ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>nul
timeout /t 1 >nul 2>nul
start "Xianyu Workbench" /min "%PYEXE%" "app\workbench.py" %*
exit /b 0

:bootstrap
set "PVER=3.12.10"
set "PDIR=%CD%\runtime"
if not exist "%PDIR%" mkdir "%PDIR%"
echo [SETUP] 1/4 Downloading Python %PVER% (portable, embeddable)...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/%PVER%/python-%PVER%-embed-amd64.zip' -OutFile \"$env:TEMP\py-embed.zip\""
if errorlevel 1 exit /b 1
echo [SETUP] 2/4 Extracting to runtime\ ...
powershell -NoProfile -Command "Expand-Archive -Force \"$env:TEMP\py-embed.zip\" '%PDIR%'"
if errorlevel 1 exit /b 1
echo [SETUP] 3/4 Enabling site-packages (python312._pth)...
powershell -NoProfile -Command "$f='%PDIR%\python312._pth'; (Get-Content $f) -replace '#\s*import site','import site' | Set-Content $f -Encoding ascii"
if errorlevel 1 exit /b 1
echo [SETUP] 4/4 Installing pip (one time)...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile \"$env:TEMP\get-pip.py\""
if errorlevel 1 exit /b 1
"%PDIR%\python.exe" "%TEMP%\get-pip.py" --no-warn-script-location
if errorlevel 1 exit /b 1
"%PDIR%\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 exit /b 1
echo [SETUP] Portable Python + pip are ready in runtime\ .
exit /b 0

:nopy
echo [SETUP] Skipped. To run this toolkit you need one of:
echo   - Python 3.9+ installed with "Add python.exe to PATH"
echo   - a portable python.exe placed in the runtime\ folder
echo   - XIANYU_PYTHON pointing to a python.exe (no quotes)
pause
exit /b 1

:bootfail
echo [ERROR] Automatic setup failed (network? proxy?). Retry, or install
echo   Python 3.9+ from https://www.python.org/downloads/ (tick "Add
echo   python.exe to PATH"), or put a portable python.exe into runtime\ .
pause
exit /b 1

:badpy
echo [ERROR] The Python found is broken or older than 3.9: %PYEXE%
echo   Fix it, or delete/bypass it so the launcher can set up its own.
pause
exit /b 1
