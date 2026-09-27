@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "data" mkdir data
if not exist "data\backups" mkdir data\backups
if not exist "logs" mkdir logs

echo [NOVA] folder: %CD%
echo [NOVA] starting collector [08:45, 12:30) interval 60s ...
start "NOVA-Collector" cmd /k python data_collector.py --interval 60 --start 08:45 --end 12:30 --weekdays-only 1^>^>logs\collector.log 2^>^>logs\collector_error.log

timeout /t 2 /nobreak >nul

echo [NOVA] starting Streamlit UI ...
start "NOVA-UI" cmd /k streamlit run app.py

echo.
echo Collector + UI launched.
echo Logs: logs\collector.log  /  logs\collector_error.log
echo Optional cleanup dry-run:
echo   python tools\cleanup_offhours.py --db data\market.sqlite
echo Apply cleanup (after backup):
echo   python tools\cleanup_offhours.py --db data\market.sqlite --apply
echo.
pause
