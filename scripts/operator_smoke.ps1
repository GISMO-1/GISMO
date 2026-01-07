$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Activate venv if not already active (optional; comment out if you prefer manual activation)
# & .\.venv\Scripts\Activate.ps1

python -c "import gismo; print('gismo import ok')" | Out-Host

$tmp = Join-Path $env:TEMP ("gismo-smoke-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $tmp | Out-Null
$db = Join-Path $tmp "state.db"

Write-Host "Using temp DB: $db"

# 1) Create profile (operator policy)
gismo memory profile create --db $db --policy policy/dev-operator.json --name operator --description "Operator defaults" --include-namespace global --include-kind preference --include-kind fact --max-items 20 --yes
if ($LASTEXITCODE -ne 0) { throw "profile create failed: $LASTEXITCODE" }

# 2) Put memory item (dev-safe policy)
gismo memory put --db $db --policy policy/dev-safe.json --namespace global --key default_model --kind preference --value-text "phi3:mini" --confidence high --source operator --yes
if ($LASTEXITCODE -ne 0) { throw "memory put failed: $LASTEXITCODE" }

# 3) Preview injection (dev-safe policy)
gismo memory preview --db $db --policy policy/dev-safe.json --memory-profile operator --json | Out-Host
if ($LASTEXITCODE -ne 0) { throw "memory preview failed: $LASTEXITCODE" }

Write-Host "OK: operator smoke passed"
