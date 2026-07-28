@echo off
REM Run the Melon Kits bot in this console window.
REM
REM Console mode: it lives and dies with this window. Closing it, logging out or rebooting
REM stops the bot, and nothing brings it back. That is a deliberate starting point, not an
REM oversight -- see HARDENING in DEPLOY-WINDOWS.md for putting it under a supervisor.
REM
REM   run.cmd                       uses C:\ProgramData\melonkit
REM   run.cmd D:\melonkit           uses another state directory

setlocal
set "STATE=%~1"
if "%STATE%"=="" set "STATE=C:\ProgramData\melonkit"

REM Resolve the checkout from this script's own location: deploy\windows\ -> kit-app\
set "KITAPP=%~dp0..\.."
for %%I in ("%KITAPP%") do set "KITAPP=%%~fI"

set "VENVPY=%KITAPP%\.venv\Scripts\python.exe"
set "CONFIG=%STATE%\melonkit.json"

if not exist "%VENVPY%" (
    echo ERROR: no virtual environment at "%VENVPY%"
    echo Run: powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1"
    exit /b 1
)
if not exist "%CONFIG%" (
    echo ERROR: no config at "%CONFIG%"
    exit /b 1
)
if "%MELONKIT_DISCORD_TOKEN%"=="" (
    echo ERROR: MELONKIT_DISCORD_TOKEN is not set in this session.
    echo Set it machine-wide as Administrator, then open a NEW console:
    echo     setx MELONKIT_DISCORD_TOKEN "your-token-here" /M
    echo setx does not affect the window it runs in, which is the usual reason this
    echo message appears twice in a row.
    exit /b 1
)

REM cd into kit-app so any relative path in the config resolves somewhere predictable.
REM The config should use absolute paths anyway - verify.ps1 fails if it does not.
cd /d "%KITAPP%"

echo Starting Melon Kits bot
echo   checkout : %KITAPP%
echo   config   : %CONFIG%
echo   state    : %STATE%
echo.
echo Ctrl-C stops it. The ledger is crash-safe, so an abrupt stop loses nothing committed.
echo.

"%VENVPY%" "%KITAPP%\bot.py" --config "%CONFIG%"
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="3" (
    echo Discord rejected the token. Check MELONKIT_DISCORD_TOKEN, and that the token was
    echo not rotated in the developer portal.
) else (
    echo Bot exited with code %CODE%.
)
endlocal & exit /b %CODE%
