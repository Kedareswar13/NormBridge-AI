<#
  Assemble a ready-to-upload Hugging Face Space folder for the NormBridge AI API.
  Usage:  powershell -ExecutionPolicy Bypass -File .\scripts\prepare_hf_space.ps1
  Output: dist\hf-space\  (Dockerfile + start.sh + backend + data + scripts + evals)
  Upload: hf upload <your-hf-username>/normbridge-api dist\hf-space . --repo-type=space
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Out = Join-Path $Root 'dist\hf-space'
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force $Out | Out-Null

Copy-Item (Join-Path $Root 'deploy\hf-space\*') $Out -Recurse
robocopy (Join-Path $Root 'backend') (Join-Path $Out 'backend') /E /NFL /NDL /NJH /NJS /NP `
  /XD .venv chroma_db uploads BitNet models pdfs __pycache__ .pytest_cache /XF *.pyc Dockerfile setup_bitnet.py | Out-Null
robocopy (Join-Path $Root 'data') (Join-Path $Out 'data') /E /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy (Join-Path $Root 'evals') (Join-Path $Out 'evals') golden_set.json heldout_set.json /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy (Join-Path $Root 'scripts') (Join-Path $Out 'scripts') *.py /NFL /NDL /NJH /NJS /NP | Out-Null

# start.sh must have LF line endings for Linux
$sh = Join-Path $Out 'start.sh'
[IO.File]::WriteAllText($sh, ([IO.File]::ReadAllText($sh) -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))

$files = (Get-ChildItem $Out -Recurse -File).Count
$mb = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Space folder ready: {0} ({1} files, {2:N1} MB)" -f $Out, $files, $mb) -ForegroundColor Green
Write-Host "Next: pip install -U huggingface_hub ; hf auth login ; hf upload <user>/normbridge-api `"$Out`" . --repo-type=space"
