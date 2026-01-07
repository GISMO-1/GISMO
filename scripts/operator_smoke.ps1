Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-GismoCli {
    param(
        [string[]]$Arguments
    )

    $output = & python -m gismo.cli.main @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $joined = ($output -join "`n")
        throw "Command failed (exit=$exitCode): python -m gismo.cli.main $($Arguments -join ' ')`n$joined"
    }
    return $output
}

function Write-Ok {
    param([string]$Message)
    Write-Host "OK: $Message"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$policyPath = Join-Path $repoRoot "policy" "dev-safe.json"

& python -c "import gismo" | Out-Null

$tmp = Join-Path $env:TEMP ("gismo-operator-" + [guid]::NewGuid().ToString())
$db = Join-Path $tmp "state.db"

New-Item -ItemType Directory -Path $tmp -Force | Out-Null
Write-Host "Using temp DB: $db"

try {
    Invoke-GismoCli @("--db", $db, "run", "echo:operator-smoke-ok", "--policy", $policyPath) | Out-Null
    Write-Ok "run ok"

    $exportOutput = Invoke-GismoCli @("--db", $db, "export", "--latest", "--policy", $policyPath)
    $exportLine = $exportOutput | Where-Object { $_ -match "^Exported run audit to" }
    if (-not $exportLine) {
        throw "Export output missing path"
    }
    if ($exportLine -notmatch "Exported run audit to\s+(.+)$") {
        throw "Export path parse failed: $exportLine"
    }
    $exportPath = $Matches[1].Trim()
    if (-not (Test-Path $exportPath)) {
        throw "Export file not found: $exportPath"
    }
    Write-Host "Export path: $exportPath"

    Write-Ok "operator smoke passed"
} finally {
    try {
        Remove-Item -Path $tmp -Recurse -Force -ErrorAction Stop
    } catch {
        throw "Failed to clean up temp directory (possible locked DB): $tmp - $($_.Exception.Message)"
    }
}
