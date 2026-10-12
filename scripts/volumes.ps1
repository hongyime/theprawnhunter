$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "deploy\volumes.ps1") @args
if (-not $?) { exit 1 }
