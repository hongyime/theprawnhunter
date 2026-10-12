param(
    [switch]$SkipDockerBuild,
    [switch]$SkipLive,
    [string]$ApiBaseUrl = $(if ($env:API_BASE_URL) { $env:API_BASE_URL } else { "http://127.0.0.1:$($env:API_PORT -as [string])" }),
    [string]$MonitorKey = $env:MONITOR_API_KEY,
    [string]$TestArgs = $env:RELEASE_GATE_TEST_ARGS,
    [string]$Python = $(if ($env:PYTHON) { $env:PYTHON } elseif (Test-Path ".\.venv-test\Scripts\python.exe") { ".\.venv-test\Scripts\python.exe" } else { "python" })
)

$ErrorActionPreference = "Stop"

if ($ApiBaseUrl -eq "http://127.0.0.1:") {
    $ApiBaseUrl = "http://127.0.0.1:8011"
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host ""
    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

if (-not $TestArgs) {
    $TestArgs = @(
        "tests/unit/test_runtime_regressions.py",
        "tests/unit/test_broadcast_failure_accounting.py",
        "tests/unit/test_queue_monitor.py",
        "tests/unit/test_scrape_result_classification.py",
        "tests/unit/test_scrape_classification_persistence.py",
        "tests/unit/test_broadcaster_hardening.py",
        "tests/unit/test_bot_listener_telemetry_command.py"
    ) -join " "
}
$TestArgList = $TestArgs -split "\s+" | Where-Object { $_ }

Invoke-Step "unit and runtime tests" {
    & $Python -m pytest @TestArgList
}

Invoke-Step "compose config" {
    docker compose config --quiet
}

if (-not $SkipDockerBuild) {
    Invoke-Step "docker build" {
        docker compose build
    }
}

if (-not $SkipLive) {
    Invoke-Step "api health" {
        & $Python scripts/ops/ops_report.py --base-url $ApiBaseUrl --monitor-key $MonitorKey
    }
}

Write-Host ""
Write-Host "Release gate passed."
