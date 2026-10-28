<#
  Build a portable zip of NormBridge AI to move to another Windows laptop.
  Usage:  powershell -ExecutionPolicy Bypass -File .\scripts\make_portable_zip.ps1 [-IncludeEnv] [-NoBitNet] [-NoDb]

  Included : source code, docs, guides, data\policies (PDFs + manifest), data\ocr_cache (so OCR never runs again),
             evals (golden + held-out sets + reports), scripts, deploy kit
             + data\db_snapshot\normbridge.sql: a snapshot of the database (policies + vectors, users, chat history,
               eval runs #1..N, audit log, traces). setup.ps1 restores it on the new machine, so the Admin eval history
               and every user's conversations come along.
  Excluded : backend\.venv, frontend\node_modules, .local (Postgres binaries/data), chroma_db, uploads, logs, caches
             -> setup.ps1 recreates all of them on the new machine.
  -IncludeEnv  also pack your .env (contains passwords - only for your own machines)
  -NoBitNet    leave out the optional backend\BitNet sources (~80 MB)
  -NoDb        don't export the database snapshot (the new machine then starts empty and re-ingests the policies)
#>
param([switch]$IncludeEnv, [switch]$NoBitNet, [switch]$NoDb)
$ErrorActionPreference = 'Continue'
$Root = Split-Path $PSScriptRoot -Parent
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$out = Join-Path (Split-Path $Root -Parent) "normbridge-portable-$stamp.zip"

# ---------------------------------------------------------------- database snapshot
$SnapDir = Join-Path $Root 'data\db_snapshot'
$Snap = Join-Path $SnapDir 'normbridge.sql'
if (-not $NoDb) {
    $cfg = @{}
    foreach ($line in Get-Content (Join-Path $Root '.env')) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
    }
    $pgDump = Join-Path $Root '.local\pgsql\bin\pg_dump.exe'
    if (-not (Test-Path $pgDump)) { $pgDump = (Get-Command pg_dump -ErrorAction SilentlyContinue).Source }
    if ($pgDump) {
        New-Item -ItemType Directory -Force $SnapDir | Out-Null
        $env:PGPASSWORD = $cfg['DB_PASSWORD']
        $dbHost = if ($cfg['DB_HOST']) { $cfg['DB_HOST'] -replace 'localhost', '127.0.0.1' } else { '127.0.0.1' }
        # --no-owner/--no-privileges: restorable by whatever role owns the database on the new machine.
        # One-time OTP codes are not worth carrying over.
        & $pgDump -h $dbHost -p $cfg['DB_PORT'] -U $cfg['DB_USER'] -d $cfg['DB_NAME'] --no-owner --no-privileges `
                  --exclude-table-data=pending_otps -E UTF8 -f $Snap
        $dumpCode = $LASTEXITCODE
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        if ($dumpCode -eq 0 -and (Test-Path $Snap)) {
            Write-Host ("[ok] database snapshot: {0:N1} MB (restored automatically by setup.ps1)" -f ((Get-Item $Snap).Length / 1MB)) -ForegroundColor Green
        } else {
            Write-Host '[!] database snapshot failed - is PostgreSQL running? (run.ps1 starts it). Packing without it.' -ForegroundColor Yellow
            Remove-Item $Snap -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host '[!] pg_dump not found - packing without a database snapshot.' -ForegroundColor Yellow
    }
} elseif (Test-Path $Snap) {
    Remove-Item $Snap   # never ship a stale snapshot by accident
}

$excludes = @(
  '--exclude', 'backend/.venv', '--exclude', 'frontend/node_modules', '--exclude', 'frontend/build',
  '--exclude', '.local', '--exclude', 'backend/chroma_db', '--exclude', 'backend/uploads',
  '--exclude', '__pycache__', '--exclude', '*.pyc', '--exclude', '.pytest_cache', '--exclude', '*.log', '--exclude', '.git', '--exclude', 'dist'
)
if (-not $IncludeEnv) { $excludes += @('--exclude', '.env') }
if ($NoBitNet) { $excludes += @('--exclude', 'backend/BitNet') }

Write-Host "Packing $Root -> $out"
Push-Location $Root
# bsdtar (built into Windows 10+) writes real zip files with -a and is much faster than Compress-Archive
tar -a -c -f $out @excludes .
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0 -or -not (Test-Path $out)) { Write-Host 'Zip failed' -ForegroundColor Red; exit 1 }
Write-Host ("Done: {0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB)) -ForegroundColor Green
if (-not $IncludeEnv) { Write-Host 'Note: .env was NOT included - setup.ps1 will create a fresh one with new passwords.' }
