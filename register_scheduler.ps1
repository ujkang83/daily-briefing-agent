$batPath = "c:\AI_Program\daily-briefing-agent\run_daily_briefing.bat"
$workingDir = "c:\AI_Program\daily-briefing-agent"
$taskName = "DailyBriefing_AutoRun"
$runTime = "06:10"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`" /silent" -WorkingDirectory $workingDir
$trigger = New-ScheduledTaskTrigger -Daily -At $runTime

try {
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "✅ [완벽 성공] 절전모드 자동 깨우기(WakeToRun)를 포함하여 등록되었습니다!"
} catch {
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "ℹ️ 일반 사용자 권한으로 안전 등록되었습니다!"
}

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "   - 작업 이름: $taskName"
Write-Host "   - 예약 주기: 매일 아침 $runTime"
Write-Host "   - 다음 실행 예정: $($info.NextRunTime)"
