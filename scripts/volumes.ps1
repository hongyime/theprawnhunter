#Requires -Version 5.1
<#
.SYNOPSIS
  Backup / restore The Prawn Hunter's Docker data volumes
  (redis_data, sessions, imports, beat_schedule).
.EXAMPLE
  .\scripts\volumes.ps1 backup            # -> .\volume-backups\*.tar.gz
  .\scripts\volumes.ps1 restore           # restore from .\volume-backups
  .\scripts\volumes.ps1 backup D:\backups
.NOTES
  Volume names are read from .env (REDIS_VOLUME_NAME etc.) when present,
  else fall back to the telegramhunter_* defaults in docker-compose.yml.
  Uses a throwaway alpine container + tar (works the same on any OS).
#>
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('backup', 'restore')]
  [string]$Action,
  [Parameter(Position = 1)]
  [string]$Dir = './volume-backups'
)
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

$volumes = @(
  (VolName 'REDIS_VOLUME_NAME' 'telegramhunter_redis_data'),
  (VolName 'SESSIONS_VOLUME_NAME' 'telegramhunter_sessions'),
  (VolName 'IMPORTS_VOLUME_NAME' 'telegramhunter_imports'),
  (VolName 'BEAT_SCHEDULE_VOLUME_NAME' 'telegramhunter_beat_schedule')
)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'docker not found on PATH' }

if ($Action -eq 'backup') {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  $abs = (Resolve-Path $Dir).Path
  Write-Host "Backing up volumes -> $abs"
  foreach ($v in $volumes) {
    docker volume inspect $v *> $null
    if ($LASTEXITCODE -ne 0) { Write-Host "  skip (not found): $v"; continue }
    Write-Host "  backup: $v"
    docker run --rm --mount "type=volume,source=$v,target=/data,readonly" --mount "type=bind,source=$abs,target=/backup" alpine tar czf "/backup/$v.tar.gz" -C /data .
    if ($LASTEXITCODE -ne 0) { throw "backup failed for $v" }
  }
  Write-Host "Done. Copy '$abs' AND your .env to the target machine (both hold secrets)."
}
else {
  if (-not (Test-Path $Dir)) { throw "backup dir not found: $Dir" }
  $abs = (Resolve-Path $Dir).Path
  Write-Host "Restoring volumes <- $abs"
  foreach ($v in $volumes) {
    $f = Join-Path $abs "$v.tar.gz"
    if (-not (Test-Path $f)) { Write-Host "  skip (no archive): $v"; continue }
    docker volume create $v | Out-Null
    Write-Host "  restore: $v"
    docker run --rm --mount "type=volume,source=$v,target=/data" --mount "type=bind,source=$abs,target=/backup" alpine tar xzf "/backup/$v.tar.gz" -C /data
    if ($LASTEXITCODE -ne 0) { throw "restore failed for $v" }
  }
  Write-Host 'Done.'
}
