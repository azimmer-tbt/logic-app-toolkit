#!/bin/bash
# check_patch.sh — "Do I even need to patch?"
# Runs generate_orthodox.py in --dry-run mode.
# Quick, non-destructive drift check.
#
# Usage:
#   ./pipeline/check_patch.sh \
#       --input-desired-values target_state/desired_values_FRESHMART-DEV.v2.txt \
#       --input-prior PRIOR/FRESHMART-DEV-PRICES.json \
#       --input-materials materials/
#
# Exit codes:
#   0 = drifted (patches needed)
#   1 = error
#   2 = clean (no patches needed)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

python3 "$SCRIPT_DIR/utilities/generators/generate_orthodox.py" \
    "$@" \
    --dry-run
