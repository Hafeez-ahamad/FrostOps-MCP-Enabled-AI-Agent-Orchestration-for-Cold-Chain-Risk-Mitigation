<#
.SYNOPSIS
    Task runner for Windows PowerShell.
.EXAMPLE
    .\tasks.ps1 test
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "db-up", "db-init", "test", "test-integration", "lint",
                 "sim", "load-kaggle", "mocks", "demo", "replay", "api", "console", "chat")]
    [string]$Task = "test",
    # Extra arguments passed to the task's command (replay, api).
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Import-DotEnv {
    # Load KEY=VALUE lines from .env into this process only.
    $path = Join-Path $Root ".env"
    if (-not (Test-Path $path)) { throw ".env not found. Copy .env.example to .env and fill it in." }
    foreach ($line in Get-Content $path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim('"'), "Process")
        }
    }
}

function Assert-Venv {
    if (-not (Test-Path $Py)) { throw "No .venv found. Run: .\tasks.ps1 setup" }
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Set-Location $Root

switch ($Task) {
    "setup" {
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "uv is required (https://docs.astral.sh/uv/). It provides Python 3.12."
        }
        Invoke-Checked "uv" @("venv", "--python", "3.12", ".venv")
        Invoke-Checked "uv" @("pip", "install", "--python", $Py, "-r", "requirements-dev.txt")
    }
    "db-up" {
        Import-DotEnv
        Invoke-Checked "docker" @("compose", "up", "-d", "--wait", "mssql")
    }
    "db-init" {
        Assert-Venv
        Import-DotEnv
        # Applies db/raw_schema.sql, db/ops_schema.sql, db/roles.sql in order (re-runnable).
        Invoke-Checked $Py @("-m", "engine.dbadmin", "init")
    }
    "test" {
        Assert-Venv
        Invoke-Checked $Py @("-m", "pytest")
    }
    "test-integration" {
        Assert-Venv
        Import-DotEnv
        & $Py -m pytest -m integration
        if ($LASTEXITCODE -eq 5) { Write-Host "No integration tests collected yet."; exit 0 }
        exit $LASTEXITCODE
    }
    "lint" {
        Assert-Venv
        Invoke-Checked $Py @("-m", "ruff", "check", ".")
        Invoke-Checked $Py @("-m", "ruff", "format", "--check", ".")
        Invoke-Checked $Py @("-m", "mypy")
    }
    "sim" {
        # Generate the smoke scenario, ingest it (append + dedup) and run the data-quality runner.
        Assert-Venv
        Import-DotEnv
        Invoke-Checked $Py @("-m", "sim", "load", "sim/scenarios/smoke.yaml")
    }
    "load-kaggle" {
        Assert-Venv
        Import-DotEnv
        Invoke-Checked $Py @("-m", "engine.data.kaggle")
    }
    "mocks" {
        # All six mock services on http://127.0.0.1:8100 (simulated; for manual demos).
        Assert-Venv
        Invoke-Checked $Py @("-m", "sim.mock_services", "--port", "8100")
    }
    "demo" {
        # Phase 3 demo: a simulated day through planner, gate and executor (no database needed).
        Assert-Venv
        Invoke-Checked $Py @("-m", "eval.phase3_demo")
    }
    "replay" {
        # e.g. .\tasks.ps1 replay -- --campaign mvp --configs C0 --seeds 1 --blocks 1
        Assert-Venv
        Invoke-Checked $Py (@("-m", "eval.replay") + $Rest)
    }
    "api" {
        # Dispatch API on a simulated day (http://127.0.0.1:8000). Prints demo tokens unless
        # API_USERS is set. e.g. .\tasks.ps1 api -- --config C0 --start-hours 10
        Assert-Venv
        Invoke-Checked $Py (@("-m", "api") + $Rest)
    }
    "console" {
        # Streamlit client of the API: start `.	asks.ps1 api` first.
        Assert-Venv
        Invoke-Checked $Py @("-m", "streamlit", "run", "console/app.py")
    }
    "chat" {
        Assert-Venv
        Invoke-Checked $Py @("-m", "engine.chat.cli")
    }
}
