@echo off
chcp 65001 >nul
title Tencent Hunyuan 3.5 To API (Port 7860)
cd /d "%~dp0"
echo ====================================================
echo  Tencent Hunyuan 3.5 To API 正在启动...
echo  本地访问: http://127.0.0.1:7860
echo  Tailscale访问: http://100.68.95.0:7860
echo  API 文档: http://127.0.0.1:7860/docs
echo ====================================================
python -m app.main
pause
