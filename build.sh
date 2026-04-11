#!/usr/bin/env bash
set -euo pipefail

VERSION=$(python3 -c "from swm import __version__; print(__version__)")
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

# Normalize arch names
case "$ARCH" in
    x86_64)  ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
esac

OUTNAME="swm-${VERSION}-${OS}-${ARCH}"
echo "Building ${OUTNAME} with Nuitka..."

python3 -m nuitka \
    --standalone \
    --onefile \
    --deployment \
    --output-filename=swm \
    --output-dir=dist \
    --include-package=swm \
    --include-package=click \
    --include-package=rich \
    --include-package=tomli_w \
    --include-package=httpx \
    --include-package=boto3 \
    --include-package=botocore \
    --include-package=certifi \
    --include-package=httpcore \
    --include-package=idna \
    --include-package=anyio \
    --include-package=sniffio \
    --include-package=h11 \
    --include-package=urllib3 \
    --include-package=jmespath \
    --include-package=dateutil \
    --include-package=s3transfer \
    --follow-imports \
    --assume-yes-for-downloads \
    --remove-output \
    swm_main.py

mkdir -p dist
if [ -f "swm" ]; then
    mv swm "dist/${OUTNAME}"
elif [ -f "dist/swm" ]; then
    mv "dist/swm" "dist/${OUTNAME}"
fi

echo ""
echo "Built: dist/${OUTNAME}"
ls -lh "dist/${OUTNAME}"

# Generate checksum
cd dist
shasum -a 256 "${OUTNAME}" > "${OUTNAME}.sha256"
echo "Checksum: $(cat ${OUTNAME}.sha256)"
