@echo off
rem Launch the Dahua ANPR Monitor with a fixed database location.
rem %~dp0 is this script's own folder, so the database is ALWAYS the same
rem file regardless of the directory you start the script from.

set "ANPR_DB=%~dp0anpr.db"
set "ANPR_PORT=8080"

echo Starting Dahua ANPR Monitor
echo   Database: %ANPR_DB%
echo   Open:     http://localhost:%ANPR_PORT%
echo.

cd /d "%~dp0"
python -m anpr
pause
