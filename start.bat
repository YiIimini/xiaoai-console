@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   小爱同学 . 大模型工作台  一键启动
echo ============================================

set "PORT=8799"
if not "%XIAOAI_PORT%"=="" set "PORT=%XIAOAI_PORT%"

rem ---------- 找 Python ----------
set "BASEPY="
for %%P in (python py python3) do (
    if not defined BASEPY (
        %%P -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
        if !errorlevel! equ 0 set "BASEPY=%%P"
    )
)
if not defined BASEPY (
    echo [xiaoai] 没找到 Python 3.9+。
    echo        请到 https://www.python.org/downloads/ 安装，安装时勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)

rem ---------- 建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [xiaoai] 首次运行，创建虚拟环境 .venv ...
    %BASEPY% -m venv .venv
    if errorlevel 1 (
        echo [xiaoai] 创建虚拟环境失败
        pause
        exit /b 1
    )
)
set "PY=.venv\Scripts\python.exe"

rem ---------- 装依赖 ----------
"%PY%" -c "import miservice" >nul 2>&1
if errorlevel 1 (
    echo [xiaoai] 安装依赖 miservice ...
    "%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo [xiaoai] 默认源失败，改用清华镜像重试 ...
        "%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
    "%PY%" -c "import miservice" >nul 2>&1
    if errorlevel 1 (
        echo [xiaoai] 依赖装不上，将以演示模式启动（接真机前请联网重跑本脚本）
    )
)

if not exist "data" mkdir data
if exist "data\port" del "data\port"

rem ---------- 启动 ----------
echo [xiaoai] 启动工作台 ...
start "" /b "%PY%" -m app.server --port %PORT% >> data\server.out 2>&1

set "URL=http://127.0.0.1:%PORT%"
for /l %%i in (1,1,60) do (
    if exist "data\port" (
        set /p PORT=<data\port
        set "URL=http://127.0.0.1:!PORT!"
        goto :ready
    )
    timeout /t 1 /nobreak >nul
)
echo [xiaoai] 启动超时，请查看 data\server.out
pause
exit /b 1

:ready
echo [xiaoai] 工作台地址: %URL%
start "" "%URL%"
echo.
echo  体检: .venv\Scripts\python.exe -m app.doctor
echo  停止: 关闭本窗口，或在任务管理器结束 python.exe
timeout /t 3 /nobreak >nul
