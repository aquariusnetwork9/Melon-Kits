@echo off
REM Pull the latest code, check it, and tell you what to do next. Windows host.
REM
REM   update.cmd            pull, update deps, run the tests
REM   update.cmd --no-test  skip the suite
REM
REM This does NOT restart the bot, because in console mode there is nothing to restart it
REM with -- the bot is a process in another window and only you can stop it. Killing it from
REM here would leave it down. So: stop the bot's window with Ctrl-C, run this, then run.cmd.
REM
REM `/version` in Discord tells you whether the running process is behind the checkout, which
REM is exactly the gap a pull-without-restart creates.

setlocal
set "KITAPP=%~dp0..\.."
for %%I in ("%KITAPP%") do set "KITAPP=%%~fI"
for %%I in ("%KITAPP%\..") do set "CHECKOUT=%%~fI"
set "VENVPY=%KITAPP%\.venv\Scripts\python.exe"

if not exist "%VENVPY%" (
    echo ERROR: no virtual environment at "%VENVPY%"
    echo Run install.ps1 first.
    exit /b 1
)

cd /d "%CHECKOUT%"
for /f %%i in ('git rev-parse --short HEAD') do set "BEFORE=%%i"
echo   at %BEFORE%, pulling...
git pull --ff-only
if errorlevel 1 (
    echo ERROR: pull failed. Resolve it by hand rather than forcing.
    exit /b 1
)
for /f %%i in ('git rev-parse --short HEAD') do set "AFTER=%%i"

if "%BEFORE%"=="%AFTER%" (
    echo   already up to date ^(%AFTER%^)
) else (
    echo   %BEFORE% -^> %AFTER%
    git --no-pager log --oneline %BEFORE%..%AFTER%
)

echo   checking dependencies...
"%VENVPY%" -m pip install --quiet --disable-pip-version-check "discord.py>=2.4,<3"

if "%~1"=="--no-test" goto :done
echo   running the test suite...
cd /d "%KITAPP%"
"%VENVPY%" -m unittest discover -s tests -q
if errorlevel 1 (
    echo.
    echo ERROR: tests FAILED. Do not restart the bot on this code.
    exit /b 1
)

:done
echo.
echo Updated to %AFTER%.
echo Now restart the bot: stop its window with Ctrl-C, then run:
echo     "%~dp0run.cmd"
echo Check it took with /version in Discord.
endlocal
