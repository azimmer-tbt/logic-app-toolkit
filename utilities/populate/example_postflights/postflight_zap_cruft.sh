#!/bin/bash
# postflight: zap_cruft.sh
# Removes common macOS/Python cruft from an unpacked directory.
# Safe to run on any kit. $1 = base directory.

BASE="${1:?Usage: postflight.sh <base_dir>}"
cd "$BASE"

find . -name ".DS_Store" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
find . -name ".AppleDouble" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "Thumbs.db" -delete 2>/dev/null || true

echo "postflight: cruft cleared from $BASE"
exit 0
