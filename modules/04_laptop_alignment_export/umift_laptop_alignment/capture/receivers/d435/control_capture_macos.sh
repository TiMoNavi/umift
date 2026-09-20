#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE04_ROOT="$(cd "${ROOT_DIR}/../../../.." && pwd)"
REPO_ROOT="$(cd "${MODULE04_ROOT}/../.." && pwd)"
MODULE01_GLOBAL_CAMERA="${REPO_ROOT}/modules/01_global_camera"
INSTALLED_GLOBAL_CAMERA="/usr/local/libexec/umift-global-camera"

if [[ -n "${UMIFT_D435_LIBREALSENSE_PREFIX:-}" ]]; then
  INSTALL_PREFIX="${UMIFT_D435_LIBREALSENSE_PREFIX}"
elif [[ -d "${MODULE01_GLOBAL_CAMERA}/third_party/librealsense-install" ]]; then
  INSTALL_PREFIX="${MODULE01_GLOBAL_CAMERA}/third_party/librealsense-install"
elif [[ -d "${INSTALLED_GLOBAL_CAMERA}/third_party/librealsense-install" ]]; then
  INSTALL_PREFIX="${INSTALLED_GLOBAL_CAMERA}/third_party/librealsense-install"
else
  INSTALL_PREFIX="${ROOT_DIR}/third_party/librealsense-install"
fi
LIB_DIR="${INSTALL_PREFIX}/lib"
PY_DIR="${INSTALL_PREFIX}/python"
CAPTURE_SCRIPT="${UMIFT_D435_CAPTURE_SCRIPT:-${ROOT_DIR}/capture_d435i.py}"
DEFAULT_SERIAL="327122071246"
STATE_DIR="${ROOT_DIR}/.runtime"
PID_FILE="${STATE_DIR}/capture.pid"
META_FILE="${STATE_DIR}/capture_session.json"
LOG_FILE="${STATE_DIR}/capture.stdout.log"
DEFAULT_STATUS_FILE="/tmp/umift_d435_stream_status.json"

mkdir -p "${STATE_DIR}"

if [[ ! -f "${CAPTURE_SCRIPT}" ]]; then
  echo "Missing capture script: ${CAPTURE_SCRIPT}"
  exit 1
fi

if [[ ! -x "${CAPTURE_SCRIPT}" ]]; then
  :
fi

if [[ ! -d "${LIB_DIR}" || ! -d "${PY_DIR}" ]]; then
  echo "Missing librealsense runtime under ${INSTALL_PREFIX}"
  echo "Run: bash modules/01_global_camera/install_realsense_macos.sh"
  exit 1
fi

export DYLD_LIBRARY_PATH="${LIB_DIR}${DYLD_LIBRARY_PATH:+:${DYLD_LIBRARY_PATH}}"
export PYTHONPATH="${PY_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

usage() {
  cat <<EOF
Usage:
  sudo bash modules/04_laptop_alignment_export/umift_laptop_alignment/capture/receivers/d435/control_capture_macos.sh <command> [args...]

Commands:
  list
      List RealSense devices.

  run-once [capture_d435i.py args...]
      Run one foreground capture.

  stream [capture_d435i.py args...]
      Run one foreground live debug stream.

  start [capture_d435i.py args...]
      Start a background capture session and store pid/session metadata.

  stop
      Stop the background capture session if one is running.

  status
      Print JSON describing the current background session.

Notes:
  - If no --serial is given, this script injects:
      --serial ${DEFAULT_SERIAL}
  - This module-04 copy runs:
      ${CAPTURE_SCRIPT}
  - librealsense runtime is searched in:
      ${MODULE01_GLOBAL_CAMERA}/third_party/librealsense-install
      ${INSTALLED_GLOBAL_CAMERA}/third_party/librealsense-install
      or UMIFT_D435_LIBREALSENSE_PREFIX
  - start/status/stop use:
      ${STATE_DIR}
EOF
}

write_session_meta() {
  local status="$1"
  local pid="$2"
  shift 2
  python3 - "$META_FILE" "$status" "$pid" "$@" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

meta_path = Path(sys.argv[1])
status = sys.argv[2]
pid = int(sys.argv[3])
args = sys.argv[4:]

payload = {
    "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "status": status,
    "pid": pid,
    "args": args,
}
meta_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
PY
}

normalize_args() {
  local args=("$@")
  local has_serial=false
  local arg
  for arg in "${args[@]}"; do
    if [[ "${arg}" == "--serial" || "${arg}" == --serial=* ]]; then
      has_serial=true
      break
    fi
  done

  if [[ "${has_serial}" == false ]]; then
    args+=(--serial "${DEFAULT_SERIAL}")
  fi

  printf '%s\n' "${args[@]}"
}

collect_normalized_args() {
  local collected=()
  local line
  while IFS= read -r line; do
    collected+=("${line}")
  done < <(normalize_args "$@")
  printf '%s\0' "${collected[@]}"
}

load_pid_if_running() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
      echo "${pid}"
      return 0
    fi
  fi
  return 1
}

COMMAND="${1:-help}"
shift || true

case "${COMMAND}" in
  help|--help|-h)
    usage
    ;;
  *)
    if [[ "$(id -u)" -ne 0 ]]; then
      echo "control_capture_macos.sh must run as root."
      exit 1
    fi
    ;;
esac

case "${COMMAND}" in
  help|--help|-h)
    ;;
  list)
    ARGS=()
    while IFS= read -r -d '' arg; do
      ARGS+=("${arg}")
    done < <(collect_normalized_args --list-devices "$@")
    exec python3 "${CAPTURE_SCRIPT}" "${ARGS[@]}"
    ;;
  run-once)
    ARGS=()
    while IFS= read -r -d '' arg; do
      ARGS+=("${arg}")
    done < <(collect_normalized_args "$@")
    exec python3 "${CAPTURE_SCRIPT}" "${ARGS[@]}"
    ;;
  stream)
    ARGS=()
    while IFS= read -r -d '' arg; do
      ARGS+=("${arg}")
    done < <(collect_normalized_args --stream "$@")
    exec python3 "${CAPTURE_SCRIPT}" "${ARGS[@]}"
    ;;
  start)
    if pid="$(load_pid_if_running)"; then
      echo "Capture already running with pid ${pid}"
      exit 1
    fi
    ARGS=()
    while IFS= read -r -d '' arg; do
      ARGS+=("${arg}")
    done < <(collect_normalized_args "$@")
    rm -f "${DEFAULT_STATUS_FILE}"
    nohup python3 "${CAPTURE_SCRIPT}" "${ARGS[@]}" >"${LOG_FILE}" 2>&1 &
    pid=$!
    echo "${pid}" > "${PID_FILE}"
    write_session_meta "running" "${pid}" "${ARGS[@]}"
    echo "started pid=${pid}"
    echo "log=${LOG_FILE}"
    ;;
  stop)
    if pid="$(load_pid_if_running)"; then
      stop_mode="graceful"
      kill "${pid}" >/dev/null 2>&1 || true
      for _ in {1..50}; do
        if ! kill -0 "${pid}" >/dev/null 2>&1; then
          break
        fi
        sleep 0.1
      done
      if kill -0 "${pid}" >/dev/null 2>&1; then
        stop_mode="forced"
        kill -KILL "${pid}" >/dev/null 2>&1 || true
      fi
      sleep 0.5
      rm -f "${DEFAULT_STATUS_FILE}"
      rm -f "${PID_FILE}"
      write_session_meta "stopped" 0
      echo "stopped pid=${pid} mode=${stop_mode}"
    else
      echo "no running capture"
      exit 1
    fi
    ;;
  status)
    if pid="$(load_pid_if_running)"; then
      write_session_meta "running" "${pid}"
    else
      rm -f "${PID_FILE}" "${DEFAULT_STATUS_FILE}"
      write_session_meta "stopped" 0
    fi
    cat "${META_FILE}"
    ;;
  *)
    echo "Unknown command: ${COMMAND}"
    echo
    usage
    exit 1
    ;;
esac
