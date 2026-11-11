# Runs every check CI runs. Skips Go or Perl if they are not installed.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Step($name, [scriptblock]$block) {
    Write-Host "`n== $name ==" -ForegroundColor Cyan
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$name failed" }
}

Step "ruff check" { python -m ruff check . }
Step "ruff format" { python -m ruff format --check . }
Step "pytest" { python -m pytest }

if (Get-Command go -ErrorAction SilentlyContinue) {
    Push-Location dns
    try {
        Step "go vet" { go vet ./... }
        Step "go test" { go test -count=1 ./... }
    }
    finally { Pop-Location }
} else { Write-Host "go not found: skipping Go checks" -ForegroundColor Yellow }

if (Get-Command prove -ErrorAction SilentlyContinue) {
    Step "perl tests" { prove analytics/t }
} else { Write-Host "perl not found: skipping Perl checks" -ForegroundColor Yellow }

Write-Host "`nAll checks passed" -ForegroundColor Green
