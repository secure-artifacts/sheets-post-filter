@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import flask,gspread" 2>nul
if errorlevel 1 (
  echo 正在安装依赖...
  python -m pip install -r requirements.txt
)
echo 正在打开数据汇总工具...
python app.py
pause
