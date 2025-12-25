@echo off
title LanEnc Server & Auto-Updater

:: 1. Navigate to the project directory
cd /d "%~dp0"

:: 2. --- AUTO UPDATE SECTION ---
echo ====================================================
echo      Checking for Updates (GitHub)
echo ====================================================
git remote update >nul 2>&1
git status -uno | find "Your branch is behind" >nul
if %ERRORLEVEL% EQU 0 (
    echo [UPDATE] New version found! Downloading...
    git pull
    echo [UPDATE] Updating dependencies...
    
    :: Activate Conda specifically for the pip install
    :: (We do the activation logic below, but need it here for pip if not active)
    :: For simplicity, we will let the main activation block handle the environment, 
    :: then run pip install before the launcher.
    set NEED_PIP=1
) else (
    echo [UPDATE] System is up to date.
    set NEED_PIP=0
)
echo.

:: 3. --- CONDA ACTIVATION ---
:: Set your environment name here
set CONDA_ENV=lanenc

echo [BOOT] Activating Environment: %CONDA_ENV%...

:: Try generic "conda" command first (if in PATH)
call conda activate %CONDA_ENV% 2>nul

:: If that failed, try common installation paths
if %ERRORLEVEL% NEQ 0 (
    if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" (
        call "%USERPROFILE%\miniconda3\Scripts\activate.bat" "%USERPROFILE%\miniconda3"
    ) else if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
        call "%USERPROFILE%\anaconda3\Scripts\activate.bat" "%USERPROFILE%\anaconda3"
    ) else if exist "C:\ProgramData\Anaconda3\Scripts\activate.bat" (
        call "C:\ProgramData\Anaconda3\Scripts\activate.bat" "C:\ProgramData\Anaconda3"
    )
    call activate %CONDA_ENV%
)

:: 4. --- DEPENDENCY CHECK ---
:: If we updated git, ensure requirements match
if "%NEED_PIP%"=="1" (
    echo [SETUP] Checking for new Python requirements...
    pip install -r requirements.txt
)

:: 5. --- LAUNCH ---
echo.
echo ====================================================
echo      Starting LanEnc Engine...
echo ====================================================
python launcher.py

:: Keep window open only if it crashes
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [CRASH] Server stopped unexpectedly.
    pause
)