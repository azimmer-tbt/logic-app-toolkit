#!/bin/bash
# postflight: verify_and_chmod.sh
# Verifies a required file list exists, makes .sh/.py files executable,
# and zaps common cruft. $1 = base directory.
# Edit REQUIRED_FILES for your kit before shipping.

set -euo pipefail
BASE="${1:?Usage: postflight.sh <base_dir>}"
cd "$BASE"

echo "=== postflight: verify + chmod ==="

REQUIRED_FILES=(
    "inquisitor.py"
    "cartographer.py"
    "modes_definition.yaml"
    "helpers/checksum.py"
)

MISSING=0
for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "  MISSING: $f"
        MISSING=$((MISSING + 1))
    else
        echo "  OK:      $f"
    fi
done

if [[ $MISSING -gt 0 ]]; then
    echo "postflight FAILED: $MISSING required file(s) missing." >&2
    exit 1
fi

find . -name "*.sh" -exec chmod +x {} \;
find . -name "*.py" -exec chmod +x {} \;
echo "  chmod +x: *.sh *.py"

find . -name ".DS_Store" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
echo "  cruft: cleared"

echo "=== postflight: PASS ==="
exit 0
