$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "deploy\apply_migrations.ps1") @args
if (-not $?) { exit 1 }
