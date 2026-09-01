@echo off
title Local Automation Test
echo Starting Flask Controller...
start cmd /k "python run.py"

echo Waiting for controller to start...
timeout /t 3 /nobreak >nul

echo Starting Worker...
start cmd /k "set CONTROLLER_URL=http://192.168.50.89:7561 && set WORKER_NAME=LocalTestWorker && set POLL_INTERVAL=2 && python worker_agent\worker.py"

echo Both processes started in new windows.
echo Dashboard available at: http://192.168.50.89:7561
pause
