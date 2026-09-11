#!/bin/bash
# Validate pytest configuration
# Ensures pytest_plugins is only defined in root conftest.py (pytest 8.x requirement)
set -e

cd fastapi_backend

echo "🔍 Checking pytest configuration..."

# Check for pytest_plugins declarations (not comments) in subdirectory conftest files
# Match actual assignment: pytest_plugins = [...] or pytest_plugins=[...]
if find tests -mindepth 2 -name "conftest.py" -exec grep -E "^[[:space:]]*pytest_plugins[[:space:]]*=" {} + | grep -q .; then
    echo "❌ ERROR: pytest_plugins declaration found in subdirectory conftest files"
    echo ""
    echo "pytest_plugins must only be defined in root conftest.py (pytest 8.x requirement)"
    echo ""
    echo "Found in:"
    find tests -mindepth 2 -name "conftest.py" -exec grep -l -E "^[[:space:]]*pytest_plugins[[:space:]]*=" {} \;
    echo ""
    echo "Move pytest_plugins declarations to: fastapi_backend/conftest.py"
    exit 1
fi

# Quick pytest collection check (unit tests only — catches conftest import errors fast)
if ! uv run pytest --collect-only -q tests/unit/ > /dev/null 2>&1; then
    echo "❌ ERROR: Pytest test collection failed"
    echo ""
    echo "Run: cd fastapi_backend && uv run pytest --collect-only"
    exit 1
fi

echo "✅ Pytest configuration valid"
