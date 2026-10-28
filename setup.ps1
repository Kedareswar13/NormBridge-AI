<#
  NormBridge AI - one-time setup for Windows (no admin rights needed).
  Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
  Re-running is safe: every step skips work that is already done.
#>
param(
    [switch]$SkipModels,      # don't pull Ollama models
    [switch]$SkipFrontend     # don't run npm install
)
$ErrorActionPreference = 'Continue'   # native tools write to stderr; failures are checked via $LASTEXITCODE
$Root  = $PSScriptRoot
$Local = Join-Path $Root '.local'
New-Item -ItemType Directory -Force $Local | Out-Null
$env:UV_HTTP_TIMEOUT = '600'
$env:UV_LINK_MODE = 'copy'   # avoids hardlink errors on OneDrive/cloud-synced folders

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!]  $m" -ForegroundColor Yellow }

function Read-DotEnv($path) {
    $h = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $h[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $h
}

function New-Secret { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ }) }

# ---------------------------------------------------------------- 1. .env
Step '.env'
$EnvFile = Join-Path $Root '.env'
if (-not (Test-Path $EnvFile)) {
    $txt = Get-Content (Join-Path $Root '.env.example') -Raw
    $txt = $txt -replace '(?m)^DB_PASSWORD=change-me', "DB_PASSWORD=$(New-Secret)"
    $txt = $txt -replace '(?m)^ADMIN_PASSWORD=change-me', "ADMIN_PASSWORD=$(New-Secret)"
    $txt = $txt -replace '(?m)^AUDIT_SALT=change-me', "AUDIT_SALT=$(New-Secret)"
    $txt = $txt -replace '(?m)^SESSION_SECRET=change-me', "SESSION_SECRET=$(New-Secret)$(New-Secret)"
    [IO.File]::WriteAllText($EnvFile, $txt)
    Ok 'created .env with random DB and admin passwords (see .env)'
} else { Ok '.env already exists' }
$cfg = Read-DotEnv $EnvFile

# ---------------------------------------------------------------- 2. Tools
Step 'Checking tools'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Warn 'uv not found - installing (user scope)'
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Ok "uv $(uv --version)"

$OllamaExe = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $OllamaExe -and (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")) { $OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not $OllamaExe -and -not $SkipModels) {
    Warn 'Ollama not found - installing with winget (user scope)'
    winget install --id Ollama.Ollama -e --scope user --silent --accept-package-agreements --accept-source-agreements
    $OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
}
if ($OllamaExe -and (Test-Path $OllamaExe)) { Ok "ollama at $OllamaExe" } else { Warn 'Ollama not installed - get it from https://ollama.com/download' }

# ---------------------------------------------------------------- 3. PostgreSQL (portable, only if no external DB)
Step 'PostgreSQL'
$useExternalDb = $cfg['DATABASE_URL'] -or ($cfg['DB_HOST'] -and $cfg['DB_HOST'] -notin @('localhost', '127.0.0.1'))
$PgBin  = Join-Path $Local 'pgsql\bin'
$PgData = Join-Path $Local 'pgdata'
if ($useExternalDb) {
    Ok 'external database configured in .env - skipping local Postgres'
} else {
    if (-not (Test-Path (Join-Path $PgBin 'pg_ctl.exe'))) {
        Warn 'downloading portable PostgreSQL 16 (~300 MB)'
        $zip = Join-Path $Local 'pg.zip'
        curl.exe -L --retry 5 -o $zip https://get.enterprisedb.com/postgresql/postgresql-16.10-1-windows-x64-binaries.zip
        # tar (built into Windows 10+) is far faster than Expand-Archive; pgAdmin/symbols are not needed
        tar -xf $zip -C $Local --exclude "pgsql/pgAdmin 4" --exclude "pgsql/symbols" --exclude "pgsql/StackBuilder"
        Remove-Item $zip
    }
    Ok "binaries in $PgBin"

    $pgPort = if ($cfg['DB_PORT']) { $cfg['DB_PORT'] } else { '5432' }
    $dbUser = $cfg['DB_USER']; $dbPass = $cfg['DB_PASSWORD']; $dbName = $cfg['DB_NAME']
    if (-not (Test-Path (Join-Path $PgData 'PG_VERSION'))) {
        $pw = Join-Path $Local 'pw.tmp'
        [IO.File]::WriteAllText($pw, $dbPass)
        & (Join-Path $PgBin 'initdb.exe') -D $PgData -U postgres --pwfile=$pw -A scram-sha-256 -E UTF8 --no-locale | Out-Null
        Remove-Item $pw
        Ok 'initialised data directory'
    }
    & (Join-Path $PgBin 'pg_ctl.exe') -D $PgData status *> $null
    if ($LASTEXITCODE -ne 0) {
        # Another Postgres (e.g. a system install) may already own the port: pick the next free one and save it.
        $busy = { param($p) [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) }
        if (& $busy $pgPort) {
            $old = $pgPort
            $pgPort = (5433..5450 | Where-Object { -not (& $busy $_) } | Select-Object -First 1).ToString()
            $envTxt = [IO.File]::ReadAllText($EnvFile) -replace '(?m)^DB_PORT=.*$', "DB_PORT=$pgPort"
            [IO.File]::WriteAllText($EnvFile, $envTxt)
            Warn "port $old is in use by another program - using $pgPort (saved to .env)"
        }
        # Start-Process (ShellExecute) passes no handles to postgres; inheriting them can hang the calling shell.
        $p = Start-Process (Join-Path $PgBin 'pg_ctl.exe') -ArgumentList "-D `"$PgData`" -l `"$(Join-Path $Local 'pg.log')`" -o `"-p $pgPort`" -w start" -WindowStyle Hidden -PassThru
        $p.WaitForExit()
        if ($p.ExitCode -ne 0) { throw "PostgreSQL failed to start - see .local\pg.log" }
    }
    Ok "server running on port $pgPort"

    $env:PGPASSWORD = $dbPass
    $psql = Join-Path $PgBin 'psql.exe'
    $hasRole = & $psql -h 127.0.0.1 -p $pgPort -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$dbUser'"
    if ($LASTEXITCODE -ne 0) { throw "cannot connect to the local PostgreSQL on port $pgPort" }
    if ($hasRole -ne '1') { & $psql -h 127.0.0.1 -p $pgPort -U postgres -c "CREATE ROLE $dbUser LOGIN PASSWORD '$dbPass'" | Out-Null }
    $hasDb = & $psql -h 127.0.0.1 -p $pgPort -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$dbName'"
    if ($hasDb -ne '1') { & $psql -h 127.0.0.1 -p $pgPort -U postgres -c "CREATE DATABASE $dbName OWNER $dbUser" | Out-Null }
    Remove-Item Env:PGPASSWORD
    Ok "role '$dbUser' and database '$dbName' ready"

    # Restore the snapshot shipped in the zip (policies + vectors, users, chat history, eval runs, audit, traces),
    # but only into a database that is still empty, so re-running setup.ps1 never overwrites newer data.
    $snapshot = Join-Path $Root 'data\db_snapshot\normbridge.sql'
    if (Test-Path $snapshot) {
        $env:PGPASSWORD = $dbPass
        $hasTables = & $psql -h 127.0.0.1 -p $pgPort -U $dbUser -d $dbName -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='policies'"
        if ($hasTables -ne '1') {
            & $psql -h 127.0.0.1 -p $pgPort -U $dbUser -d $dbName -q -v ON_ERROR_STOP=1 -f $snapshot *> (Join-Path $Local 'db_restore.log')
            if ($LASTEXITCODE -eq 0) {
                $counts = & $psql -h 127.0.0.1 -p $pgPort -U $dbUser -d $dbName -tAc "SELECT (SELECT count(*) FROM policies)||' policies, '||(SELECT count(*) FROM eval_runs)||' eval runs, '||(SELECT count(*) FROM conversations)||' conversations'"
                Ok "database snapshot restored: $counts"
            } else { Warn "snapshot restore failed - see .local\db_restore.log (run.ps1 -Ingest will rebuild the policies instead)" }
        } else { Ok 'database already has data - snapshot not restored (delete .local\pgdata to start over)' }
        Remove-Item Env:PGPASSWORD
    }
}

# ---------------------------------------------------------------- 4. Backend venv
Step 'Backend Python environment (3.11)'
$Backend = Join-Path $Root 'backend'
$Py = Join-Path $Backend '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { uv venv (Join-Path $Backend '.venv') --python 3.11 }
uv pip install --python $Py torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python $Py -r (Join-Path $Backend 'requirements.txt')
Ok 'backend dependencies installed'

# ---------------------------------------------------------------- 5. Ollama models
if (-not $SkipModels -and $OllamaExe -and (Test-Path $OllamaExe)) {
    Step 'Ollama models'
    $up = curl.exe -s -o NUL -m 3 -w "%{http_code}" http://localhost:11434/api/tags 2>$null
    if ($up -ne '200') { Start-Process $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden; Start-Sleep 5 }
    $chat = if ($cfg['OLLAMA_MODEL']) { $cfg['OLLAMA_MODEL'] } else { 'qwen2.5:1.5b' }
    $emb  = if ($cfg['EMBEDDING_MODEL']) { $cfg['EMBEDDING_MODEL'] } else { 'nomic-embed-text' }
    & $OllamaExe pull $emb
    & $OllamaExe pull $chat
    & $OllamaExe pull qwen2.5:3b   # optional higher-quality model, selectable from the chat's model menu
    $ocr = if ($cfg['OLLAMA_OCR_MODEL']) { $cfg['OLLAMA_OCR_MODEL'] } else { 'glm-ocr' }
    & $OllamaExe pull $ocr
    Ok "models: $chat, $emb, $ocr"
}

# ---------------------------------------------------------------- 6. Frontend
if (-not $SkipFrontend) {
    Step 'Frontend (npm install)'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Warn 'Node.js not found - install Node 22 LTS: winget install OpenJS.NodeJS.LTS --scope user'
    } else {
        Push-Location (Join-Path $Root 'frontend')
        npm install --no-audit --no-fund
        Pop-Location
        Ok "node $(node --version)"
    }
}

Write-Host "`nSetup complete. Start everything with:  powershell -ExecutionPolicy Bypass -File .\run.ps1" -ForegroundColor Green
