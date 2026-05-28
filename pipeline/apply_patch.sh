#!/bin/bash
# apply_patch.sh — "Do the surgery."
# Runs surgeon with the orthodox YAML against the PRIOR JSON.
# Optionally runs inquisitor to verify the result.
# The output is what you paste into Azure Portal.
#
# Usage:
#   ./pipeline/apply_patch.sh \
#       --input-prior PRIOR/FRESHMART-DEV-PRICES.json \
#       --input-orthodox target_state/FRESHMART-DEV.orthodox.yaml \
#       --output-patched CURRENT/patched__FRESHMART-DEV-PRICES.json \
#       --logfile-surgeon CURRENT/surgeon_FRESHMART-DEV.log \
#       [--input-inquisitor target_state/FRESHMART-DEV.inquisitor.yaml] \
#       [--logfile-inquisitor CURRENT/inquisitor_FRESHMART-DEV.log]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

INPUT_PRIOR=""
INPUT_ORTHODOX=""
OUTPUT_PATCHED=""
LOGFILE_SURGEON=""
INPUT_INQUISITOR=""
LOGFILE_INQUISITOR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input-prior) INPUT_PRIOR="$2"; shift 2;;
        --input-orthodox) INPUT_ORTHODOX="$2"; shift 2;;
        --output-patched) OUTPUT_PATCHED="$2"; shift 2;;
        --logfile-surgeon) LOGFILE_SURGEON="$2"; shift 2;;
        --input-inquisitor) INPUT_INQUISITOR="$2"; shift 2;;
        --logfile-inquisitor) LOGFILE_INQUISITOR="$2"; shift 2;;
        *) echo "Unknown flag: $1"; exit 1;;
    esac
done

for VAR in INPUT_PRIOR INPUT_ORTHODOX OUTPUT_PATCHED LOGFILE_SURGEON; do
    if [ -z "${!VAR}" ]; then
        echo "Missing required flag. Usage:"
        echo "  --input-prior PATH --input-orthodox PATH"
        echo "  --output-patched PATH --logfile-surgeon PATH"
        exit 1
    fi
done

echo "=== Applying patches ==="
python3 "$SCRIPT_DIR/surgeon.py" \
    --input "$INPUT_PRIOR" \
    --patch-task "$INPUT_ORTHODOX" \
    --output "$OUTPUT_PATCHED" \
    --log "$LOGFILE_SURGEON"

if [ $? -ne 0 ]; then
    echo "Surgeon failed. See: $LOGFILE_SURGEON"
    exit 1
fi

echo "Surgeon passed. Output: $OUTPUT_PATCHED"

if [ -n "$INPUT_INQUISITOR" ] && [ -n "$LOGFILE_INQUISITOR" ]; then
    echo "=== Running inquisitor ==="
    python3 "$SCRIPT_DIR/inquisitor.py" \
        --input "$OUTPUT_PATCHED" \
        --check "$INPUT_INQUISITOR" \
        --log "$LOGFILE_INQUISITOR"
    echo "   Inquisitor: done"
fi

echo ""
echo "Next: paste $OUTPUT_PATCHED into Azure Portal"
