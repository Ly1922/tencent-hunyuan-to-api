@echo off
chcp 65001 >nul
echo 正在停止占用 7860 端口的进程...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":7860" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a
)
echo 已停止。
timeout /t 2 >nul
