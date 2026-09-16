@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

echo ========================================================
echo   [데일리 브리핑] 윈도우 작업 스케줄러 자동 등록기
echo ========================================================
echo.
echo 매일 아침 05:47 정각에 데일리 브리핑이 자동 실행되도록
echo 윈도우 작업 스케줄러(DailyBriefing_AutoRun)에 등록합니다.
echo.

set TASK_NAME=DailyBriefing_AutoRun
set BAT_PATH=c:\AI_Program\daily-briefing-agent\run_daily_briefing.bat
set WORKING_DIR=c:\AI_Program\daily-briefing-agent
set RUN_TIME=05:47

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$batPath = '%BAT_PATH%';" ^
  "$workingDir = '%WORKING_DIR%';" ^
  "$taskName = '%TASK_NAME%';" ^
  "$runTime = '%RUN_TIME%';" ^
  "$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c \"\"' + $batPath + '\" /silent\"') -WorkingDirectory $workingDir;" ^
  "$trigger = New-ScheduledTaskTrigger -Daily -At $runTime;" ^
  "try {" ^
  "    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew;" ^
  "    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null;" ^
  "    Write-Host '✅ [완벽 성공] 절전모드 자동 깨우기(WakeToRun)를 포함하여 등록되었습니다!';" ^
  "} catch {" ^
  "    Write-Host 'ℹ️ 일반 사용자 권한으로 안전 등록을 진행합니다...';" ^
  "    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew;" ^
  "    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null;" ^
  "    Write-Host '✅ [성공] 작업 스케줄러에 등록되었습니다!';" ^
  "}" ^
  "$info = Get-ScheduledTaskInfo -TaskName $taskName;" ^
  "Write-Host ('   - 작업 이름: ' + $taskName);" ^
  "Write-Host ('   - 예약 주기: 매일 아침 ' + $runTime);" ^
  "Write-Host ('   - 다음 실행 예정: ' + $info.NextRunTime);"

echo.
echo 등록이 완료되었습니다. 아무 키나 누르면 창을 닫습니다...
pause > nul
