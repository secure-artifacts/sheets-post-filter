@echo off
chcp 65001 >nul
echo Uninstalling...
taskkill /IM "数据汇总工具.exe" /F >nul 2>&1
del /f /q "%USERPROFILE%\Desktop\数据汇总工具.lnk" >nul 2>&1
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\数据汇总工具.lnk" >nul 2>&1
cd /d "%TEMP%"
rmdir /s /q "%LOCALAPPDATA%\sheets-post-filter"
echo Done.
pause
