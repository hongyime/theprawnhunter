$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "ops\run_fofa_overnight.ps1") @args
if (-not $?) { exit 1 }
