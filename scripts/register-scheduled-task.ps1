<#
.SYNOPSIS
  매일 자동 실행(`python -m pipeline scheduled`)을 Windows 작업 스케줄러에 등록한다 (ADR-025).

.DESCRIPTION
  **사람이 직접 실행한다.** 머신 상태를 바꾸는 일이라 에이전트가 대신 돌리지 않는다.

  등록 전에 확인하는 것:
    - 상시 승인 기록(.claude/scheduled-run-approved.json)이 있는가 — 없으면 등록은 되지만
      매 실행이 사전 점검에서 멈춘다(종료 코드 4). 그래서 여기서 먼저 멈춘다.
    - 실행 시각: -Time 이 없으면 config.yaml 의 schedule.time 을 읽는다. 둘 다 없으면 멈춘다.

  설정:
    - StartWhenAvailable — PC 가 꺼져 있어 놓친 실행은 켜진 뒤 따라잡는다
    - 로그온한 경우에만 실행 — 토스트가 사용자 세션에 떠야 하고, 암호를 저장하지 않는다
    - 동시 실행 금지(IgnoreNew) — 코드의 잠금 파일과 이중으로 막는다
    - 실행 시간 한도 3시간

  VPN 이 필요한 환경이면 schedule.readiness.wait_minutes 를 VPN 이 붙는 시간보다 길게 둔다.
  그 시간 안에 엔드포인트에 닿지 않으면 "환경 미준비"(종료 코드 5)로 끝나고 차단기를 쓰지 않는다.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-scheduled-task.ps1 -Time 09:30
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-scheduled-task.ps1 -Unregister
#>
param(
  [string]$Time,
  [string]$TaskName = "ai-news-ontology daily",
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'

if ($Unregister) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Output "해제함: $TaskName"
  exit 0
}

if (-not (Test-Path $python)) { throw "가상환경이 없다: $python" }
if (-not (Test-Path (Join-Path $repo '.claude\scheduled-run-approved.json'))) {
  throw "상시 승인 기록이 없다. 먼저 수동 전량 실행(일반 게이트) → 결과 확인 → '.venv\Scripts\python.exe -m pipeline approve-schedule' (ADR-025)"
}

if (-not $Time) {
  $Time = & $python -c "import yaml; print((yaml.safe_load(open('config.yaml', encoding='utf-8')).get('schedule') or {}).get('time') or '')"
  $Time = "$Time".Trim()
}
if ($Time -notmatch '^\d{1,2}:\d{2}$') { throw "실행 시각이 없다. -Time HH:MM 을 주거나 config.yaml 의 schedule.time 을 채운다" }

$action = New-ScheduledTaskAction -Execute $python -Argument '-m pipeline scheduled' -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description 'ai-news-ontology 매일 자동 실행 (ADR-025). 상시 승인과 대조 후 config 상한으로 돈다' -Force | Out-Null

Write-Output "등록함: $TaskName — 매일 $Time, 작업 디렉터리 $repo"
Write-Output "확인: Get-ScheduledTaskInfo -TaskName '$TaskName'"
