@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

:: 작업 디렉토리를 이 배치 파일이 위치한 폴더로 이동
cd /d "%~dp0"

set LOGFILE=briefing_run.log
set ERRFILE=error.log
set PYTHON_EXE=c:\AI_Program\.venv\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python
)

echo ======================================================= >> "%LOGFILE%"
echo [%date% %time%] 데일리 브리핑 파이프라인 가동 시작 >> "%LOGFILE%"
echo ======================================================= >> "%LOGFILE%"

echo [데일리 브리핑] 작업을 시작합니다... (로그: %LOGFILE%)

"%PYTHON_EXE%" main.py >> "%LOGFILE%" 2>&1
set EXITCODE=%errorlevel%

if %EXITCODE% EQU 0 (
    echo [%date% %time%] ✅ 데일리 브리핑 성공 완료 (종료코드: 0) >> "%LOGFILE%"
    echo [데일리 브리핑] ✅ 성공적으로 완료되었습니다!
) else (
    echo [%date% %time%] ❌ 데일리 브리핑 실행 실패 (종료코드: %EXITCODE%) >> "%LOGFILE%"
    echo [%date% %time%] ❌ 데일리 브리핑 실행 실패 (종료코드: %EXITCODE%) >> "%ERRFILE%"
    echo [데일리 브리핑] ❌ 실행 중 오류가 발생했습니다. (상세 내용은 %LOGFILE% 참조)
)

:: 스케줄러 백그라운드 호출(/silent)이 아닐 때만 5초 대기
if /i not "%1"=="/silent" (
    timeout /t 5 > nul
)

exit /b %EXITCODE%
