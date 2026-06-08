@echo off
setlocal enabledelayedexpansion

:: Switch to the directory where this .bat file is located
cd /d "%~dp0"

:: 1. Smart auto-update
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
        echo updates available!

        :: Check for divergence (Force Push Detection)
        :: We find the common ancestor. If the ancestor is NOT the local HEAD,
        :: it means the remote has branched off elsewhere (force push).
        for /f "tokens=*" %%a in ('git merge-base !LOCAL! !REMOTE!') do set ANCESTOR=%%a

        if /i "!ANCESTOR!" NEQ "!LOCAL!" (
            echo [!] Divergence detected (possible force push).
            echo [!] Resetting local branch to match remote...
            git stash
            git reset --hard origin/%(git rev-parse --abbrev-ref @{u}) 2>nul
            :: Since we can't easily run git commands inside a set command in Batch,
            :: we use a small trick to get the branch name for the reset.
            for /f "tokens=*" %%b in ('git rev-parse --abbrev-ref @{u}') do set BRANCH=%%b
            git reset --hard origin/!BRANCH!
            git stash pop || echo note: some local changes could not be automatically reapplied.
        ) else (
            echo pulling changes...
            git stash
            git pull
            git stash pop || echo note: some local changes could not be automatically reapplied.
        )
    ) else (
        echo already up to date.
    )
) else (
    echo warning: git fetch failed. skipping update check.
)

:: 2. Ensure virtual environment and dependencies are up to date
if not exist "venv" (
    if "%PYTHON_BIN%"=="" (
        echo error: PYTHON_BIN environment variable is not set.
        pause
        exit /b 1
    )
    echo setting up virtual environment with %PYTHON_BIN%...
    %PYTHON_BIN% -m venv venv
)

echo ensuring dependencies are up to date...
:: Use the python executable inside the venv directly
venv\Scripts\python.exe -m pip install -q --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo done!
pause
