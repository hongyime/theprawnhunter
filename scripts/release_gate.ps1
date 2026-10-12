$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "deploy\release_gate.ps1") @args
if (-not $?) { exit 1 }
