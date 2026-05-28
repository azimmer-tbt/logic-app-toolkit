#!/bin/bash
# bootstrap.sh — One-time setup for the logic-app-toolkit.
# Creates a venv and installs from vendored wheels. No network required.
#
# Usage:
#   ./bootstrap.sh
#
# Prerequisites:
#   Python 3.12+ (brew install python@3.12 if needed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Logic App Toolkit Bootstrap ==="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found."
    echo ""
    echo "Install via Homebrew:"
    echo "  brew install python@3.12"
    echo ""
    echo "If you don't have Homebrew:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

echo "Found Python ${PY_VERSION}"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 12 ]; }; then
    echo "ERROR: Python 3.12+ required, found ${PY_VERSION}"
    echo "  brew install python@3.12"
    exit 1
fi

# Check vendor/ exists
if [ ! -d "$SCRIPT_DIR/vendor" ] || [ -z "$(ls -A "$SCRIPT_DIR/vendor/"*.whl 2>/dev/null)" ]; then
    echo "ERROR: vendor/ directory is empty or missing."
    echo "Run vendor_wheels.sh on a machine with PyPI access first."
    exit 1
fi

# Create venv
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    python3 -m venv "$SCRIPT_DIR/.venv"
    echo "Created .venv"
else
    echo "Using existing .venv"
fi

source "$SCRIPT_DIR/.venv/bin/activate"

# Install from vendored wheels — NO NETWORK
echo ""
echo "=== Installing from vendored wheels ==="
pip install --no-index --find-links "$SCRIPT_DIR/vendor/" -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "Bootstrap complete."
echo ""
echo "To activate the environment:"
echo "  source .venv/bin/activate"
echo ""
echo "Quick checks:"
echo "  python3 surgeon.py --version"
echo "  python3 utilities/generators/desired_values_parser.py --help"
echo "  pytest --version"
