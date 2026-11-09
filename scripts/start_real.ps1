param([int]$Port = 8016)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUTF8 = '1'
& '.\.venv\Scripts\python.exe' 'scripts/configure_real.py'
if ($LASTEXITCODE -ne 0) { throw 'Configuration failed' }
& docker compose --env-file .env.real -f docker-compose.real.yml up -d --wait --wait-timeout 480
if ($LASTEXITCODE -ne 0) { throw 'Infrastructure is not ready' }
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --env-file .env.real --host 127.0.0.1 --port $Port
