$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "deploy\setup_cloudflare_tunnel.ps1") @args
if (-not $?) { exit 1 }
