<#
.SYNOPSIS
    One-time Cloudflare Tunnel setup for theprawnhunter honeypot receiver.
    Binds winnethepooh.hong-yi.me → localhost:8011 (API + honeypot receiver).

.DESCRIPTION
    After running this script:
    1. A browser window opens for CF auth (one-time)
    2. A named tunnel "prawnhunter" is created
    3. DNS CNAME winnethepooh.hong-yi.me → <tunnel-id>.cfargotunnel.com is added
    4. A Windows service is registered so the tunnel persists across reboots

.NOTES
    Prerequisites:
    - cloudflared installed (winget install Cloudflare.cloudflared)
    - hong-yi.me domain on your Cloudflare account
    - Docker API running on localhost:8011
#>

$ErrorActionPreference = "Stop"
$CLOUDFLARED = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$TUNNEL_NAME = "prawnhunter"
$HOSTNAME = "winnethepooh.hong-yi.me"
$LOCAL_URL = "http://localhost:8011"
$CONFIG_DIR = "$env:USERPROFILE\.cloudflared"

Write-Host "=== Step 1: Login to Cloudflare (opens browser) ===" -ForegroundColor Cyan
if (-not (Test-Path "$CONFIG_DIR\cert.pem")) {
    & $CLOUDFLARED tunnel login
    if ($LASTEXITCODE -ne 0) { throw "Login failed" }
} else {
    Write-Host "Already logged in (cert.pem exists)" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Step 2: Create named tunnel ===" -ForegroundColor Cyan
$existing = & $CLOUDFLARED tunnel list --output json 2>$null | ConvertFrom-Json
$tunnel = $existing | Where-Object { $_.name -eq $TUNNEL_NAME }
if ($tunnel) {
    $TUNNEL_ID = $tunnel.id
    Write-Host "Tunnel '$TUNNEL_NAME' already exists: $TUNNEL_ID" -ForegroundColor Green
} else {
    & $CLOUDFLARED tunnel create $TUNNEL_NAME
    if ($LASTEXITCODE -ne 0) { throw "Tunnel creation failed" }
    $existing = & $CLOUDFLARED tunnel list --output json | ConvertFrom-Json
    $tunnel = $existing | Where-Object { $_.name -eq $TUNNEL_NAME }
    $TUNNEL_ID = $tunnel.id
    Write-Host "Created tunnel: $TUNNEL_ID" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Step 3: Write tunnel config ===" -ForegroundColor Cyan
$configPath = "$CONFIG_DIR\config.yml"
$configContent = @"
tunnel: $TUNNEL_ID
credentials-file: $CONFIG_DIR\$TUNNEL_ID.json

ingress:
  - hostname: $HOSTNAME
    service: $LOCAL_URL
    originRequest:
      noTLSVerify: true
  - service: http_status:404
"@
$configContent | Set-Content $configPath -Encoding UTF8
Write-Host "Config written to $configPath"
Get-Content $configPath

Write-Host ""
Write-Host "=== Step 4: Route DNS ===" -ForegroundColor Cyan
try {
    & $CLOUDFLARED tunnel route dns $TUNNEL_NAME $HOSTNAME
} catch {
    Write-Host "DNS route may already exist (non-fatal): $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Step 5: Install as Windows service ===" -ForegroundColor Cyan
try {
    & $CLOUDFLARED service install
    Write-Host "Service installed. Will auto-start on boot." -ForegroundColor Green
} catch {
    Write-Host "Service install failed (may already exist): $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Step 6: Start tunnel ===" -ForegroundColor Cyan
& $CLOUDFLARED tunnel run $TUNNEL_NAME
# This blocks — the tunnel runs in foreground. If installed as service,
# you can skip this and just do: Start-Service cloudflared
