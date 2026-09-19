@echo off
cd /d "%~dp0"
pip install -r requirements.txt
pip show pyinstaller >nul 2>nul || pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --icon app.ico "--add-data=app.ico;." "--add-data=app_icon.png;." --collect-all customtkinter --name QQReminder main.py
echo.
echo Done. Output: dist\QQReminder.exe
pause
