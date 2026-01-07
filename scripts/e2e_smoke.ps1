param(
    [switch]$EnableMemoryPreview
)

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
$operatorPolicyPath = Join-Path $repoRoot "policy" "dev-operator.json"

& python -c "import gismo" | Out-Null

$tmp = Join-Path $env:TEMP ("gismo-e2e-" + [guid]::NewGuid().ToString())
$db = Join-Path $tmp "state.db"

New-Item -ItemType Directory -Path $tmp -Force | Out-Null
Write-Host "Using temp DB: $db"

try {
    Invoke-GismoCli @("--db", $db, "enqueue", "echo:smoke-e2e-ok") | Out-Null
    Write-Ok "enqueue ok"

    Invoke-GismoCli @("--db", $db, "daemon", "--policy", $policyPath, "--once") | Out-Null
    Write-Ok "daemon once ok"

    $statsOutput = Invoke-GismoCli @("--db", $db, "queue", "stats", "--json")
    $statsJson = ($statsOutput -join "`n") | ConvertFrom-Json
    $byStatus = $statsJson.by_status
    if ($byStatus.QUEUED -ne 0 -or $byStatus.IN_PROGRESS -ne 0 -or $byStatus.FAILED -ne 0) {
        throw "Queue not empty: QUEUED=$($byStatus.QUEUED) IN_PROGRESS=$($byStatus.IN_PROGRESS) FAILED=$($byStatus.FAILED)"
    }
    if ($byStatus.SUCCEEDED -lt 1) {
        throw "Queue item did not succeed (SUCCEEDED=$($byStatus.SUCCEEDED))"
    }
    Write-Ok "queue stats ok"

    $runsOutput = Invoke-GismoCli @("--db", $db, "runs", "list", "--limit", "1")
    $runsLine = $runsOutput | Where-Object { $_ -match "^Runs:" }
    if (-not $runsLine) {
        throw "Runs list did not include summary line"
    }
    if ($runsLine -notmatch "Runs:\s+(\d+)") {
        throw "Runs list summary parse failed: $runsLine"
    }
    $runsCount = [int]$Matches[1]
    if ($runsCount -lt 1) {
        throw "No runs recorded"
    }
    Write-Ok "runs list ok"

    if ($EnableMemoryPreview) {
        if (-not (Test-Path $operatorPolicyPath)) {
            throw "Operator policy not found: $operatorPolicyPath"
        }
        Invoke-GismoCli @(
            "--db",
            $db,
            "memory",
            "profile",
            "create",
            "--name",
            "smoke-e2e",
            "--description",
            "Smoke e2e profile",
            "--include-namespace",
            "run:*",
            "--include-kind",
            "note",
            "--max-items",
            "5",
            "--policy",
            $operatorPolicyPath,
            "--yes"
        ) | Out-Null
        Invoke-GismoCli @(
            "--db",
            $db,
            "memory",
            "put",
            "--namespace",
            "run:smoke",
            "--key",
            "seed",
            "--kind",
            "note",
            "--value-text",
            "smoke",
            "--confidence",
            "high",
            "--source",
            "operator",
            "--policy",
            $policyPath,
            "--yes"
        ) | Out-Null
        $previewOutput = Invoke-GismoCli @(
            "--db",
            $db,
            "memory",
            "preview",
            "--memory-profile",
            "smoke-e2e",
            "--policy",
            $operatorPolicyPath,
            "--json"
        )
        $previewPath = Join-Path $tmp "memory_preview.json"
        ($previewOutput -join "`n") | Set-Content -Path $previewPath -Encoding utf8

        $runId = & python -c "from gismo.core.state import StateStore; s=StateStore(r'$db'); run=s.get_latest_run(); print(run.id if run else ''); s.close()"
        if (-not $runId) {
            throw "Unable to resolve latest run id for memory preview"
        }
        $recordScript = @'
import json
import sys
from gismo.memory.store import record_event, policy_hash_for_path

preview_path = sys.argv[1]
run_id = sys.argv[2]
db_path = sys.argv[3]
policy_path = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None

with open(preview_path, "r", encoding="utf-8") as handle:
    preview = json.load(handle)

record_event(
    db_path,
    operation="memory.inject",
    actor="operator_smoke",
    policy_hash=policy_hash_for_path(policy_path),
    request={"source": preview.get("source"), "profile": preview.get("profile")},
    result_meta=preview,
    related_run_id=run_id,
)
print(run_id)
'@
        & python -c $recordScript $previewPath $runId $db $operatorPolicyPath | Out-Null
        Write-Ok "memory preview trace recorded"
    }

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
    if ($EnableMemoryPreview) {
        $memoryEvent = Get-Content $exportPath | Where-Object { $_ -match '"record_type"\s*:\s*"memory_event"' -and $_ -match '"operation"\s*:\s*"memory.inject"' }
        if (-not $memoryEvent) {
            throw "Export did not include memory.inject event"
        }
        Write-Ok "memory inject event present"
    }

    Write-Ok "e2e smoke passed"
} finally {
    try {
        Remove-Item -Path $tmp -Recurse -Force -ErrorAction Stop
    } catch {
        throw "Failed to clean up temp directory (possible locked DB): $tmp - $($_.Exception.Message)"
    }
}
