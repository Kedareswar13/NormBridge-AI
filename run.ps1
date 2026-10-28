<#
  NormBridge AI - start everything (Postgres, Ollama, backend, frontend).
  Usage:  powershell -ExecutionPolicy Bypass -File .\run.ps1 [-Ingest] [-NoFrontend]
    -Ingest      upload the policy PDFs in data\policies once the backend is up
    -NoFrontend  start only the API
  Stop:   powershell -ExecutionPolicy Bypass -File .\run.ps1 -Stop
#>
param([switch]$Ingest, [switch]$NoFrontend, [switch]$Stop)
$ErrorActionPreference = 'Continue'
$Root  = $PSScriptRoot
$Local = Join-Path $Root '.local'
$PgBin = Join-Path $Local 'pgsql\bin'
$PgData = Join-Path $Local 'pgdata'
$Logs = Join-Path $Local 'logs'
New-Item -ItemType Directory -Force $Logs | Out-Null

$cfg = @{}
foreach ($line in Get-Content (Join-Path $Root '.env')) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
}
$ApiPort = if ($cfg['PORT']) { $cfg['PORT'] } else { '5001' }
$PidFile = Join-Path $Local 'pids.txt'

if ($Stop) {
    if (Test-Path $PidFile) {
        # /T kills the whole tree: the venv python.exe is a launcher that spawns the real interpreter.
        foreach ($p in Get-Content $PidFile) { taskkill /PID ([int]$p) /T /F 2>$null | Out-Null }
        Remove-Item $PidFile
    }
    if (Test-Path (Join-Path $PgBin 'pg_ctl.exe')) { & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData stop -m fast 2>$null | Out-Null }
    Write-Host 'Stopped NormBridge AI.' -ForegroundColor Green
    return
}

function Wait-Url($url, $seconds) {
    # curl.exe ignores the WinINET/corporate proxy that can make Invoke-WebRequest fail for localhost
    for ($i = 0; $i -lt $seconds; $i++) {
        $code = curl.exe -s -o NUL -m 2 -w "%{http_code}" $url 2>$null
        if ($code -and $code -ne '000') { return $true }
        Start-Sleep 1
    }
    return $false
}

# Postgres (portable copy only)
if (Test-Path (Join-Path $PgBin 'pg_ctl.exe')) {
    & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData status *> $null
    if ($LASTEXITCODE -ne 0) {
        $pgPort = if ($cfg['DB_PORT']) { $cfg['DB_PORT'] } else { '5432' }
        $p = Start-Process (Join-Path $PgBin 'pg_ctl.exe') -ArgumentList "-D `"$PgData`" -l `"$(Join-Path $Local 'pg.log')`" -o `"-p $pgPort`" -w start" -WindowStyle Hidden -PassThru
        $p.WaitForExit()
    }
    Write-Host '[ok] PostgreSQL running' -ForegroundColor Green
}

# Ollama
$OllamaExe = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $OllamaExe) { $OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not (Wait-Url 'http://localhost:11434/api/tags' 1)) {
    Start-Process $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden
    if (Wait-Url 'http://localhost:11434/api/tags' 20) { Write-Host '[ok] Ollama started' -ForegroundColor Green } else { Write-Host '[!] Ollama did not start' -ForegroundColor Yellow }
} else { Write-Host '[ok] Ollama already running' -ForegroundColor Green }

# Backend
$pids = @()
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if (Get-NetTCPConnection -LocalPort ([int]$ApiPort) -State Listen -ErrorAction SilentlyContinue) {
    # Windows lets a second server bind the same port (SO_REUSEADDR) - requests would hit the stale one.
    Write-Host "[!] Port $ApiPort is already in use - run '.\run.ps1 -Stop' first (or close the old API window)." -ForegroundColor Yellow
    return
}
$be = Start-Process (Join-Path $Root 'backend\.venv\Scripts\python.exe') -ArgumentList 'serve.py' -WorkingDirectory (Join-Path $Root 'backend') `
      -RedirectStandardOutput (Join-Path $Logs 'backend.log') -RedirectStandardError (Join-Path $Logs 'backend.err.log') -WindowStyle Hidden -PassThru
$pids += $be.Id
Write-Host "... starting API (first start loads models, can take ~1 min). Logs: .local\logs\backend*.log"
if (Wait-Url "http://localhost:$ApiPort/health" 180) { Write-Host "[ok] API on http://localhost:$ApiPort" -ForegroundColor Green }
else { Write-Host '[!] API not responding yet - check .local\logs\backend.err.log' -ForegroundColor Yellow }

if ($Ingest) {
    & (Join-Path $Root 'backend\.venv\Scripts\python.exe') (Join-Path $Root 'scripts\ingest_policies.py') --api "http://localhost:$ApiPort"
}

# Frontend
if (-not $NoFrontend) {
    $env:REACT_APP_API_URL = "http://localhost:$ApiPort"
    $env:BROWSER = 'none'
    $fe = Start-Process cmd.exe -ArgumentList '/c', 'npm start' -WorkingDirectory (Join-Path $Root 'frontend') `
          -RedirectStandardOutput (Join-Path $Logs 'frontend.log') -RedirectStandardError (Join-Path $Logs 'frontend.err.log') -WindowStyle Hidden -PassThru
    $pids += $fe.Id
    if (Wait-Url 'http://localhost:3000' 180) { Write-Host '[ok] UI on http://localhost:3000' -ForegroundColor Green }
    else { Write-Host '[!] UI not responding yet - check .local\logs\frontend*.log' -ForegroundColor Yellow }
}

# Save PIDs as soon as they exist so -Stop always works
$pids | Set-Content $PidFile
Write-Host "`nNormBridge AI is running. Stop with: powershell -ExecutionPolicy Bypass -File .\run.ps1 -Stop" -ForegroundColor Green
