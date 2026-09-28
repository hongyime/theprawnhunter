#Requires -Version 5.1
<#
.SYNOPSIS
  One-shot setup of The Prawn Hunter on a fresh Windows machine.
.EXAMPLE
  .\scripts\bootstrap.ps1                    # fresh start
  .\scripts\bootstrap.ps1 .\volume-backups   # restore data volumes first
.NOTES
  With a backup dir, data volumes (Telethon sessions etc.) are restored first.
  Without one, the stack starts fresh — add accounts later via /starthunter.
  Supabase is external, so the DB comes along via the same SUPABASE_* creds.
#>
param([Parameter(Position = 0)][string]$VolumeBackupDir)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Get-EnvVal([string]$Name) {
  if (Test-Path .env) {
    $m = Select-String -Path .env -Pattern "^$Name=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($m) { return ($m.Line -replace "^$Name=", '').Trim().Trim('"') }
  }
  return $null
}
function VolName([string]$Name, [string]$Default) { $v = Get-EnvVal $Name; if ($v) { $v } else { $Default } }

Write-Host '== 1/5 Prerequisites =='
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'docker not found on PATH' }
docker compose version *> $null; if ($LASTEXITCODE -ne 0) { throw 'docker compose v2 not found' }
docker info *> $null; if ($LASTEXITCODE -ne 0) { throw 'docker daemon not reachable - is Docker Desktop running?' }
Write-Host '  ok'

Write-Host '== 2/5 .env =='
if (-not (Test-Path .env)) {
  Write-Host '  .env is missing (gitignored, does NOT clone).' -ForegroundColor Yellow
  Write-Host '  Copy the real .env from your source machine, or: Copy-Item .env.template .env  (then fill secrets)'
  throw '.env required'
}
Write-Host '  found .env'

Write-Host '== 3/5 External volumes =='
$volumes = @(
  (VolName 'REDIS_VOLUME_NAME' 'telegramhunter_redis_data'),
  (VolName 'SESSIONS_VOLUME_NAME' 'telegramhunter_sessions'),
  (VolName 'IMPORTS_VOLUME_NAME' 'telegramhunter_imports'),
  (VolName 'BEAT_SCHEDULE_VOLUME_NAME' 'telegramhunter_beat_schedule')
)
foreach ($v in $volumes) { docker volume create $v | Out-Null; Write-Host "  ok: $v" }

if ($VolumeBackupDir) {
  Write-Host "== 3b/5 Restore volume backups from '$VolumeBackupDir' =="
  & (Join-Path $PSScriptRoot 'volumes.ps1') restore $VolumeBackupDir
}

Write-Host '== 4/5 Build + start core (redis, api, worker, beat) =='
docker compose up -d --build

Write-Host '== 5/5 Verify API health =='
$port = Get-EnvVal 'API_PORT'; if (-not $port) { $port = '8011' }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://localhost:$port/health/" | Out-Null; $ok = $true; break }
  catch { Start-Sleep -Seconds 5 }
}
if ($ok) { Write-Host "  API healthy: http://localhost:$port/health/" }
else { Write-Host '  API not healthy yet - check: docker compose logs -f api' -ForegroundColor Yellow }

Write-Host ''
Write-Host 'Done. Core stack is up.'
Write-Host '  Status:       docker compose ps'
Write-Host '  Add accounts: /starthunter in Telegram (if you did not restore a sessions volume)'
Write-Host '  Extras:       docker compose --profile full up -d      (bot, flower, frontend)'
Write-Host '                docker compose --profile honeypot up -d  (Cloudflare Tunnel)'
