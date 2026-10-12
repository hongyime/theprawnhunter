$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "deploy\bootstrap.ps1") @args
if (-not $?) { exit 1 }
