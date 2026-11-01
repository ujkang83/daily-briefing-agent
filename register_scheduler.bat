@echo off
chcp 65001 > nul
setlocal

echo ========================================================
echo   [데일리 브리핑] 윈도우 작업 스케줄러 자동 등록기
echo ========================================================
echo.
echo 매일 아침 06:10 정각에 데일리 브리핑이 자동 실행되도록
echo 윈도우 작업 스케줄러에 등록합니다...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_scheduler.ps1"

echo.
echo 등록 작업이 완료되었습니다. 아무 키나 누르면 창을 닫습니다...
pause > nul
