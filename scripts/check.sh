#!/usr/bin/env sh
# Runs every check CI runs (Linux, macOS, WSL, Git Bash).
set -eu
cd "$(dirname "$0")/.."

python3 -m ruff check .
python3 -m ruff format --check .
python3 -m pytest

if command -v go >/dev/null 2>&1; then
    (cd dns && test -z "$(gofmt -l .)" && go vet ./... && go test -count=1 ./...)
else
    echo "go not found: skipping Go checks"
fi

if command -v prove >/dev/null 2>&1; then
    prove analytics/t
else
    echo "perl not found: skipping Perl checks"
fi

echo "All checks passed"
