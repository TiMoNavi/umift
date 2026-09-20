#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "install_sudoers_macos.sh is for macOS only."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_CONTROL_SCRIPT="${ROOT_DIR}/control_capture_macos.sh"
SOURCE_CAPTURE_SCRIPT="${ROOT_DIR}/capture_d435i.py"
SOURCE_RUNTIME_DIR="${ROOT_DIR}/third_party/librealsense-install"
INSTALL_ROOT="/usr/local/libexec/umift-global-camera"
INSTALLED_CONTROL_SCRIPT="${INSTALL_ROOT}/control_capture_macos.sh"
SUDOERS_FILE="/private/etc/sudoers.d/umift_global_camera"
TARGET_USER="$(id -un)"
TMP_DIR="$(mktemp -d)"
TMP_SUDOERS_FILE="${TMP_DIR}/umift_global_camera"
TMP_BUNDLE_DIR="${TMP_DIR}/bundle"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT INT TERM

if [[ ! -f "${SOURCE_CONTROL_SCRIPT}" ]]; then
  echo "Missing control script: ${SOURCE_CONTROL_SCRIPT}"
  exit 1
fi

if [[ ! -f "${SOURCE_CAPTURE_SCRIPT}" ]]; then
  echo "Missing capture script: ${SOURCE_CAPTURE_SCRIPT}"
  exit 1
fi

if [[ ! -d "${SOURCE_RUNTIME_DIR}" ]]; then
  echo "Missing runtime directory: ${SOURCE_RUNTIME_DIR}"
  echo "Run: bash modules/01_global_camera/install_realsense_macos.sh"
  exit 1
fi

if ! command -v visudo >/dev/null 2>&1; then
  echo "visudo not found."
  exit 1
fi

cat > "${TMP_SUDOERS_FILE}" <<EOF
# Installed by UMIFT global camera helper on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
Cmnd_Alias UMIFT_GLOBAL_CAMERA = ${INSTALLED_CONTROL_SCRIPT}
${TARGET_USER} ALL = (root) NOPASSWD: UMIFT_GLOBAL_CAMERA
EOF

echo "This will install a root-owned RealSense control entrypoint and a narrow sudoers whitelist."
echo "You should only need to enter your macOS password once during this setup."

mkdir -p "${TMP_BUNDLE_DIR}/third_party"
install -m 755 "${SOURCE_CONTROL_SCRIPT}" "${TMP_BUNDLE_DIR}/control_capture_macos.sh"
install -m 755 "${SOURCE_CAPTURE_SCRIPT}" "${TMP_BUNDLE_DIR}/capture_d435i.py"
cp -R "${SOURCE_RUNTIME_DIR}" "${TMP_BUNDLE_DIR}/third_party/librealsense-install"

visudo -cf "${TMP_SUDOERS_FILE}"

sudo rm -rf "${INSTALL_ROOT}"
sudo install -d -o root -g wheel -m 755 "${INSTALL_ROOT}"
sudo cp -R "${TMP_BUNDLE_DIR}/." "${INSTALL_ROOT}/"
sudo chown -R root:wheel "${INSTALL_ROOT}"
sudo find "${INSTALL_ROOT}" -type d -exec chmod 755 {} +
sudo chmod 755 "${INSTALLED_CONTROL_SCRIPT}" "${INSTALL_ROOT}/capture_d435i.py"
sudo install -d -o root -g wheel -m 755 "/private/etc/sudoers.d"
sudo install -o root -g wheel -m 440 "${TMP_SUDOERS_FILE}" "${SUDOERS_FILE}"
sudo grep -Eq '^[#@]includedir[[:space:]]+/private/etc/sudoers\.d$' /private/etc/sudoers
sudo visudo -cf "${SUDOERS_FILE}"

echo
echo "Passwordless sudo is now configured for:"
echo "  ${INSTALLED_CONTROL_SCRIPT}"
echo
echo "You can now use:"
echo "  bash modules/01_global_camera/run_capture_macos.sh list"
echo "  bash modules/01_global_camera/run_capture_macos.sh start --output-dir /tmp/realsense_bg --duration-s 30"
echo "  bash modules/01_global_camera/run_capture_macos.sh stop"
