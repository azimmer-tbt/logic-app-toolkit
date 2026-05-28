#!/bin/bash
# verify_patch.sh — "Prove it worked."
# Launches the TUI verifier or report mode.
# (verify.py is a future deliverable — this wrapper is ready for it)
#
# Usage:
#   ./pipeline/verify_patch.sh \
#       --input-patched CURRENT/patched__FRESHMART-DEV-PRICES.json \
#       --input-desired-values target_state/desired_values_FRESHMART-DEV.v2.txt \
#       --input-materials materials/ \
#       [--report]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

VERIFY_SCRIPT="$SCRIPT_DIR/confessor.py"

python3 "$VERIFY_SCRIPT" "$@"
