# Helper: apply migrations sequentially via Supabase Management API.
# One-off — token is read from env, never echoed.
param(
    [Parameter(Mandatory=$true)][string]$Ref,
    [Parameter(Mandatory=$true)][string]$Token,
    [Parameter(Mandatory=$true)][string[]]$Files
)

$headers = @{ 'Authorization' = "Bearer $Token"; 'Content-Type' = 'application/json' }
$url = "https://api.supabase.com/v1/projects/$Ref/database/query"

$applied = 0
$failed = 0

foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file)) {
        Write-Host "SKIP: $file (not found)"
        continue
    }
    $sql = Get-Content -LiteralPath $file -Raw
    $body = @{ query = $sql } | ConvertTo-Json -Compress
    Write-Host -NoNewline "APPLY: $(Split-Path -Leaf $file)..."
    try {
        $r = Invoke-RestMethod -Method Post -Uri $url -Headers $headers -Body $body -ErrorAction Stop
        Write-Host " OK"
        $applied++
    } catch {
        $errBody = $_.ErrorDetails.Message
        if (-not $errBody) { $errBody = $_.Exception.Message }
        # Truncate to keep output tight
        $short = $errBody -replace '\s+',' '
        if ($short.Length -gt 250) { $short = $short.Substring(0, 250) + '...' }
        Write-Host " FAIL: $short"
        $failed++
    }
}

Write-Host ""
Write-Host "applied: $applied  failed: $failed"
