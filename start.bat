@echo off
setlocal
set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "DIR=%USERPROFILE%\voice-logger"

if not exist "%PY%" (
    if exist "%DIR%\python_path.txt" set /p PY=<"%DIR%\python_path.txt"
)

cd /d "%DIR%"

echo Stopping old processes...

powershell -NoProfile -WindowStyle Hidden -Command "$env:PYTHONUTF8='1'; Get-CimInstance Win32_Process -Filter 'name=''python.exe''' | Where-Object { $_.CommandLine -like '*recorder.py*' -or $_.CommandLine -like '*dashboard_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 5; Start-Process -FilePath '%PY%' -ArgumentList 'recorder.py' -WorkingDirectory '%DIR%' -WindowStyle Hidden -PassThru; Start-Process -FilePath '%PY%' -ArgumentList 'dashboard_server.py' -WorkingDirectory '%DIR%' -WindowStyle Hidden -PassThru; $obs = '$env:LOCALAPPDATA\Programs\obsidian\Obsidian.exe'; if (Test-Path $obs) { $running = Get-Process -Name 'Obsidian' -ErrorAction SilentlyContinue; if (-not $running) { Start-Process -FilePath $obs } }"

timeout /t 2 /nobreak >nul
start "" "http://localhost:7331"

echo Voice Logger started.
