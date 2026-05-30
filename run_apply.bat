@echo off
cd /d %~dp0
echo ============================================
echo  CoI Apply Patch
echo ============================================
echo.

if not exist pending_update.sql (
    echo ОШИБКА: pending_update.sql не найден.
    echo Сначала запусти run_update.bat
    pause
    exit /b 1
)

set DB=postgresql://postgres:postgres@127.0.0.1:5432/coi_public

echo Применяем pending_update.sql к базе %DB%...
echo.
psql %DB% -f pending_update.sql -v ON_ERROR_STOP=1
if errorlevel 1 (
    echo.
    echo ОШИБКА при применении патча. База данных не изменена (транзакция откатилась).
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Патч успешно применён!
echo ============================================
pause
