@echo off
rem Launches Cascade Transferinator without a console window.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0cascade_transferinator.py" & exit /b)
where pyw >nul 2>nul && (start "" pyw "%~dp0cascade_transferinator.py" & exit /b)
echo Python 3 with Tk was not found on PATH. Install Python from python.org (tick "Add to PATH"),
echo or use CascadeTransferinator.exe instead.
pause
