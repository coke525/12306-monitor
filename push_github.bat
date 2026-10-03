@echo off
echo ============================================
echo  12306 monitor - push to GitHub
echo ============================================
echo.
echo Step 1: create an EMPTY PRIVATE repo at https://github.com/new
echo Step 2: paste its URL below (like https://github.com/yourname/12306-monitor.git)
echo.
set /p REPO=Repo URL:
if "%REPO%"=="" (
  echo No URL given, exit.
  pause
  exit /b 1
)
cd /d "%~dp0"
git remote remove origin 2>nul
git remote add origin %REPO%
git branch -M main
git push -u origin main
echo.
echo Done. Now go to repo Settings - Secrets and actions - Actions
echo and add secret: WXPUSHER_SPT
echo Then Actions tab - Run workflow to test.
pause
