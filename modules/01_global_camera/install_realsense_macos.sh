#!/usr/bin/env bash
set -euo pipefail

USER_BIN="${HOME}/Library/Python/3.9/bin"
if [[ -d "${USER_BIN}" ]]; then
  export PATH="${USER_BIN}:${PATH}"
fi

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This helper is for macOS only."
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required."
  exit 1
fi

if ! command -v cmake >/dev/null 2>&1; then
  echo "cmake is required. Install Xcode Command Line Tools and CMake first."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THIRD_PARTY_DIR="${ROOT_DIR}/third_party"
SRC_DIR="${THIRD_PARTY_DIR}/librealsense"
BUILD_DIR="${SRC_DIR}/build"
INSTALL_PREFIX="${THIRD_PARTY_DIR}/librealsense-install"
PY_INSTALL_DIR="${INSTALL_PREFIX}/python"

mkdir -p "${THIRD_PARTY_DIR}"

if [[ ! -d "${SRC_DIR}/.git" ]]; then
  git clone https://github.com/realsenseai/librealsense.git "${SRC_DIR}"
else
  git -C "${SRC_DIR}" pull --ff-only
fi

cmake -S "${SRC_DIR}" -B "${BUILD_DIR}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_EXAMPLES=false \
  -DBUILD_GRAPHICAL_EXAMPLES=false \
  -DBUILD_PYTHON_BINDINGS=true \
  -DPYTHON_EXECUTABLE="$(command -v python3)" \
  -DCMAKE_INSTALL_PREFIX="${INSTALL_PREFIX}"

cmake --build "${BUILD_DIR}" --config Release -j"$(sysctl -n hw.ncpu)"
cmake --install "${BUILD_DIR}" --prefix "${INSTALL_PREFIX}" || true

PY_WRAPPER_SO="$(find "${BUILD_DIR}" -path '*pyrealsense2*.so' -print -quit)"
if [[ -z "${PY_WRAPPER_SO}" ]]; then
  echo "Could not locate built pyrealsense2 shared library under ${BUILD_DIR}"
  exit 1
fi
PY_WRAPPER_DIR="$(dirname "${PY_WRAPPER_SO}")"
mkdir -p "${PY_INSTALL_DIR}"
cp "${PY_WRAPPER_DIR}"/pyrealsense2*.so "${PY_INSTALL_DIR}/"
cp "${PY_WRAPPER_DIR}"/pyrsutils*.so "${PY_INSTALL_DIR}/"

cat <<EOF

librealsense build finished.

1. Add CLI tools to PATH for this shell:
   export PATH="${INSTALL_PREFIX}/bin:\$PATH"

2. Add the Python wrapper to PYTHONPATH:
   export PYTHONPATH="${PY_INSTALL_DIR}:\$PYTHONPATH"

3. Verify:
   DYLD_LIBRARY_PATH="${INSTALL_PREFIX}/lib" rs-enumerate-devices
   python3 -c "import pyrealsense2 as rs; print(rs.context().devices)"

4. Then run a 10-second test capture:
   DYLD_LIBRARY_PATH="${INSTALL_PREFIX}/lib" \\
   python3 modules/01_global_camera/capture_d435i.py \\
     --output-dir /tmp/realsense_test \\
     --duration-s 10

Official references:
  https://github.com/realsenseai/librealsense
  https://github.com/realsenseai/librealsense/blob/master/doc/installation_osx.md
  https://github.com/realsenseai/librealsense/blob/master/wrappers/python/readme.md
EOF
