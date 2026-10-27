#!/usr/bin/env bash
# Build the aarch64 webrtcvad wheel on a device (under /tmp, no root changes) and fetch it.
# Usage: scripts/release/build-webrtcvad-wheel.sh <device-ip> [version]
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

DEVICE_IP="${1:-}"
VERSION="${2:-2.0.10}"
DEVICE_USER="${DEVICE_USER:-orangepi}"
DEVICE_PASS="${DEVICE_PASS:-orangepi}"
OUT_DIR="${WHEEL_DIR:-${ROOT_DIR}/dist/webrtcvad}"
REMOTE_WORK="/tmp/webrtcvad-wheel-build"

if [[ -z "$DEVICE_IP" ]]; then
  echo "Usage: $0 <device-ip> [version]"
  exit 1
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "Error: sshpass not found — brew install hudochenkov/sshpass/sshpass"
  exit 1
fi

SSH=(sshpass -p "$DEVICE_PASS" ssh -o StrictHostKeyChecking=no "${DEVICE_USER}@${DEVICE_IP}")
SCP=(sshpass -p "$DEVICE_PASS" scp -o StrictHostKeyChecking=no)

echo "========== Building webrtcvad ${VERSION} on ${DEVICE_IP} =========="

"${SSH[@]}" "echo ${DEVICE_PASS} | sudo -S bash -s" <<REMOTE
set -euo pipefail
UV=/root/.local/bin/uv
[ -x "\$UV" ] || { echo "uv not found at \$UV"; exit 1; }

rm -rf ${REMOTE_WORK}
mkdir -p ${REMOTE_WORK}
cd ${REMOTE_WORK}
export UV_LINK_MODE=copy

# The sdist is plain C (the WebRTC VAD sources ship inside it); setuptools comes
# from PyPI and the device's own C compiler does the build. No apt packages.
"\$UV" venv --python 3.12 .venv
"\$UV" pip install --python ${REMOTE_WORK}/.venv/bin/python pip setuptools wheel

# --no-binary names the package explicitly, NOT ":all:", so build dependencies
# still install from wheels.
${REMOTE_WORK}/.venv/bin/python -m pip wheel --no-binary webrtcvad --no-deps \
  --wheel-dir ${REMOTE_WORK}/dist "webrtcvad==${VERSION}"

ls -la ${REMOTE_WORK}/dist
REMOTE

mkdir -p "$OUT_DIR"
echo "========== Fetching wheel → ${OUT_DIR} =========="
"${SCP[@]}" "${DEVICE_USER}@${DEVICE_IP}:${REMOTE_WORK}/dist/webrtcvad-*.whl" "$OUT_DIR/"

echo "========== Verifying the wheel installs (clean venv on device) =========="
"${SSH[@]}" "echo ${DEVICE_PASS} | sudo -S bash -s" <<REMOTE
set -euo pipefail
UV=/root/.local/bin/uv
export UV_LINK_MODE=copy
rm -rf ${REMOTE_WORK}/verify
"\$UV" venv --python 3.12 ${REMOTE_WORK}/verify
"\$UV" pip install --python ${REMOTE_WORK}/verify/bin/python ${REMOTE_WORK}/dist/webrtcvad-*.whl
# Check the native extension directly: webrtcvad.py imports pkg_resources,
# which a bare 3.12 venv does not have.
${REMOTE_WORK}/verify/bin/python -c "import _webrtcvad; v = _webrtcvad.create(); _webrtcvad.init(v); print('import OK:', _webrtcvad.valid_rate_and_frame_length(16000, 480))"
REMOTE

echo "========== Cleaning up the device =========="
"${SSH[@]}" "echo ${DEVICE_PASS} | sudo -S rm -rf ${REMOTE_WORK}"

echo
ls -la "$OUT_DIR"
echo
echo "Next: make upload-webrtcvad-wheel"
