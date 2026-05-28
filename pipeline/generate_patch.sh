#!/bin/bash
# generate_patch.sh — "Build me the instructions."
# Wraps generate_orthodox.py with progress messages and next-step guidance.
#
# Usage:
#   ./pipeline/generate_patch.sh \
#       --input-desired-values target_state/desired_values_FRESHMART-DEV.v2.txt \
#       --input-prior PRIOR/FRESHMART-DEV-PRICES.json \
#       --input-materials materials/ \
#       --output-orthodox target_state/FRESHMART-DEV.orthodox.yaml

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

echo "=== Generating orthodox YAML ==="
set +e
python3 "$SCRIPT_DIR/utilities/generators/generate_orthodox.py" "$@"
EXIT=$?
set -e

if [ $EXIT -eq 0 ]; then
    echo ""
    echo "Next: ./pipeline/apply_patch.sh ..."
elif [ $EXIT -eq 2 ]; then
    echo ""
    echo "No patches needed — current state matches desired."
else
    echo ""
    echo "Error generating patches. Check output above."
    exit 1
fi
