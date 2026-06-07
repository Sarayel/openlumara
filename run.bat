@echo off
setlocal enabledelayedexpansion

:: Switch to the directory where this .bat file is located
cd /d "%~dp0"

:: 1. Detect Python binary
where python >nul 2>nul
if %errorlevel% equ 0 (
    set PYTHON_BIN=python
) else (
    echo error: no python binary found. please make sure python is installed and in your PATH.
    pause
    exit /b 1
)

:: 2. Set up virtual environment if needed
if not exist "venv" (
    echo setting up virtual environment with %PYTHON_BIN%...
    %PYTHON_BIN% -m venv venv
    venv\Scripts\pip install -r requirements.txt
)

:: 3. Smart auto-update
echo checking for updates...
git fetch origin >nul 2>nul
if %errorlevel% equ 0 (
    :: Get local HEAD
    for /f "tokens=*" %%i in ('git rev-parse HEAD') do set LOCAL=%%i
    
    :: Get remote HEAD (using @{u} for upstream)
    set REMOTE=
    for /f "tokens=*" %%i in ('git rev-parse @{u} 2^>nul') do set REMOTE=%%i

    if "!REMOTE!"=="" (
        echo no upstream configured, skipping update check.
    ) else if "!LOCAL!" NEQ "!REMOTE!" (
        echo updates available! pulling changes...
        :: Stash local changes to tracked files to prevent pull conflicts.
        :: config.yml and data/ are in .gitignore, so they remain untouched.
        git stash
        git pull
        git stash pop || echo note: some local changes could not be automatically reapplied.
    ) else (
        echo already up to date.
    )
) else (
    echo warning: git fetch failed. skipping update check.
)

:: 4. Run the app
echo starting openlumara...
venv\Scripts\python main.py %*

if %errorlevel% neq 0 (
    echo.
    echo an error occurred while running the application.
    pause
)
