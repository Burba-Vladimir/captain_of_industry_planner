@echo off
cd /d %~dp0
echo ============================================
echo  CoI Game Data Update
echo ============================================
echo.

echo [1/2] Парсим данные с вики...
python coi_parser.py
if errorlevel 1 (
    echo ОШИБКА при парсинге. Прерываем.
    pause
    exit /b 1
)

echo.
echo [2/2] Генерируем инкрементальный SQL...
python coi_diff.py
if errorlevel 1 (
    echo ОШИБКА при генерации SQL. Прерываем.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Готово! Открой pending_update.sql,
echo  проверь изменения и запусти run_apply.bat
echo ============================================
pause
