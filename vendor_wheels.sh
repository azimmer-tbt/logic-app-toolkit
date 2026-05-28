#!/bin/bash
# vendor_wheels.sh — Download wheels for air-gapped install.
# Run this on a machine WITH PyPI access whenever requirements.txt changes.
# Downloads wheels for macOS arm64 + x86_64, Python 3.12–3.14.
#
# Usage:
#   ./vendor_wheels.sh
#
# Output:
#   vendor/ directory with .whl files ready for bootstrap.sh

set -euo pipefail

VENDOR_DIR="$(cd "$(dirname "$0")" && pwd)/vendor"

echo "=== Cleaning vendor/ ==="
rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"

REQUIREMENTS="$(cd "$(dirname "$0")" && pwd)/requirements.txt"

if [ ! -f "$REQUIREMENTS" ]; then
    echo "ERROR: requirements.txt not found"
    exit 1
fi

# Download platform-specific wheels for each supported Python version.
# Pure-python wheels (py3-none-any) are deduped automatically by pip —
# only pyyaml has C extensions that need per-version downloads.

PYTHON_VERSIONS=("3.12" "3.13" "3.14")
PLATFORMS=("macosx_14_0_arm64" "macosx_14_0_x86_64")

for PY_VER in "${PYTHON_VERSIONS[@]}"; do
    for PLATFORM in "${PLATFORMS[@]}"; do
        echo "=== Downloading for Python ${PY_VER} / ${PLATFORM} ==="
        pip3 download \
            -r "$REQUIREMENTS" \
            --dest "$VENDOR_DIR" \
            --python-version "$PY_VER" \
            --platform "$PLATFORM" \
            --only-binary=:all:
    done
done

WHEEL_COUNT=$(ls "$VENDOR_DIR"/*.whl 2>/dev/null | wc -l | tr -d ' ')
echo ""
echo "Vendored ${WHEEL_COUNT} wheels in vendor/"
echo "Commit vendor/ to the repo."
