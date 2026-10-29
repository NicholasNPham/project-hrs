@echo off
rem Build HRS.exe into dist\HRS\. Run from the project folder: build.bat
rem
rem Tests run first and a failure stops the build, so a broken HRS.exe is
rem never made. dist\HRS\ is deleted and rebuilt each time: copy it to where
rem it will run and keep config.yaml and logs\ there, not in dist\HRS\.

setlocal
cd /d "%~dp0"

rem The project's virtual environment if there is one, else python on PATH.
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Run: "%PY%" -m pip install -r requirements.txt
    exit /b 1
)

echo Running tests...
"%PY%" -m pytest -q
if errorlevel 1 (
    echo Tests failed. Nothing was built.
    exit /b 1
)

rem --onedir rather than --onefile: it starts faster, antivirus flags it less,
rem and config.yaml and logs\ sit in plain view beside HRS.exe.
rem keyring finds its Windows backend through package metadata, which
rem PyInstaller leaves out unless asked; without it HRS would refuse to start
rem rather than read Credential Manager. --collect-all selenium brings
rem selenium-manager.exe, which finds a chromedriver to match Chrome.
echo Building...
"%PY%" -m PyInstaller --noconfirm --clean --onedir --console --name HRS ^
    --hidden-import keyring.backends.Windows ^
    --copy-metadata keyring ^
    --collect-all selenium ^
    main.py
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

for %%F in (config.example.yaml install_task.ps1) do (
    copy /y "%%F" "dist\HRS\%%F" >nul
    if errorlevel 1 (
        echo Built, but %%F could not be copied into dist\HRS.
        exit /b 1
    )
)

echo.
echo Built dist\HRS\HRS.exe
echo Copy the whole dist\HRS folder to where HRS will run, then beside HRS.exe:
echo   1. Copy config.example.yaml to config.yaml and fill it in.
echo   2. Run HRS.exe --setup as the Windows account that will run it.
echo   3. As that account: powershell -ExecutionPolicy Bypass -File .\install_task.ps1
endlocal
