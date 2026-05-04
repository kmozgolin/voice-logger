@echo off
echo Stopping Voice Logger...

:: Stop by PID files first (clean shutdown)
if exist "%USERPROFILE%\voice-logger\logs\recorder.pid" (
    set /p RPID=<"%USERPROFILE%\voice-logger\logs\recorder.pid"
    taskkill /f /pid %RPID% >nul 2>&1
    del "%USERPROFILE%\voice-logger\logs\recorder.pid" >nul 2>&1
    echo  + Recorder stopped
)

:: Stop dashboard by port 7331
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":7331 " ^| findstr "LISTENING"') do (
    taskkill /f /pid %%p >nul 2>&1
    echo  + Dashboard stopped
)

:: Fallback: kill by window title
taskkill /f /fi "WINDOWTITLE eq Recorder*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Dashboard*" >nul 2>&1

echo Done. All Voice Logger processes stopped.
timeout /t 2 /nobreak >nul
