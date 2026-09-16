@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

echo ========================================================
echo   [데일리 브리핑] 윈도우 작업 스케줄러 등록 해제기
echo ========================================================
echo.
echo 등록되어 있는 데일리 브리핑 자동 실행 작업(DailyBriefing_AutoRun)을
echo 윈도우 작업 스케줄러에서 안전하게 삭제합니다.
echo.

set TASK_NAME=DailyBriefing_AutoRun

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try {" ^
  "    Unregister-ScheduledTask -TaskName '%TASK_NAME%' -Confirm:$false -ErrorAction Stop;" ^
  "    Write-Host '✅ 작업 스케줄러(%TASK_NAME%)가 성공적으로 삭제되었습니다!';" ^
  "} catch {" ^
  "    Write-Host 'ℹ️ 등록된 %TASK_NAME% 작업을 찾을 수 없거나 이미 삭제되었습니다.';" ^
  "}"

echo.
echo 완료되었습니다. 아무 키나 누르면 창을 닫습니다...
pause > nul
