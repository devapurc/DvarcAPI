$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creando entorno virtual..."
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
}

$hostValue = if ($env:DVARC_HOST) { $env:DVARC_HOST } else { "127.0.0.1" }
$portValue = if ($env:DVARC_PORT) { $env:DVARC_PORT } else { "8765" }

Write-Host "Dvarc API en http://$hostValue`:$portValue  (docs: /docs)"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host $hostValue --port $portValue --reload
