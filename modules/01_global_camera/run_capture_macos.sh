#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "run_capture_macos.sh is for macOS only."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_SCRIPT="${ROOT_DIR}/control_capture_macos.sh"
INSTALL_SUDOERS_SCRIPT="${ROOT_DIR}/install_sudoers_macos.sh"
INSTALLED_CONTROL_ROOT="/usr/local/libexec/umift-global-camera"
INSTALLED_CONTROL_SCRIPT="${INSTALLED_CONTROL_ROOT}/control_capture_macos.sh"

if [[ ! -x "${CONTROL_SCRIPT}" && ! -f "${CONTROL_SCRIPT}" ]]; then
  echo "Missing control script: ${CONTROL_SCRIPT}"
  exit 1
fi

if [[ ! -f "${INSTALL_SUDOERS_SCRIPT}" ]]; then
  echo "Missing sudoers installer: ${INSTALL_SUDOERS_SCRIPT}"
  exit 1
fi

usage() {
  cat <<EOF
Usage:
  bash modules/01_global_camera/run_capture_macos.sh <command> [args...]

What this wrapper does:
  1. Routes all RealSense actions through one fixed control entrypoint
  2. Automatically uses passwordless sudo if it has already been installed
  3. Falls back to a normal sudo prompt if passwordless sudo is not ready
  4. Provides short commands for common capture tasks

Commands:
  install-passwordless-sudo
      One-time setup.
      Installs a root-owned control bundle under:
        ${INSTALLED_CONTROL_ROOT}
      Then writes a narrow sudoers whitelist for that fixed entrypoint.

  warmup-sudo
      Check whether passwordless sudo is already enabled.
      If not, refresh sudo credentials once and exit.

  list
      List RealSense devices.

  stream [duration_s] [preview_dir]
      Run a foreground live debug stream.
      Defaults: duration_s=10, preview_dir=/tmp/realsense_stream_preview

  test [duration_s] [output_dir]
      Run a short capture test.
      Defaults: duration_s=5, output_dir=/tmp/realsense_test

  start [capture_d435i.py args...]
      Start a background capture session.

  stop
      Stop the background capture session.

  status
      Show background capture session state.

  capture [capture_d435i.py args...]
      Pass remaining args through to capture_d435i.py.

  help
      Show this message.

Examples:
  bash modules/01_global_camera/run_capture_macos.sh warmup-sudo
  bash modules/01_global_camera/run_capture_macos.sh list
  bash modules/01_global_camera/run_capture_macos.sh stream
  bash modules/01_global_camera/run_capture_macos.sh stream 15 /tmp/realsense_live
  bash modules/01_global_camera/run_capture_macos.sh test
  bash modules/01_global_camera/run_capture_macos.sh test 8 /tmp/d435_test
  bash modules/01_global_camera/run_capture_macos.sh start --run-dir runs/<run_id> --duration-s 30
  bash modules/01_global_camera/run_capture_macos.sh status
  bash modules/01_global_camera/run_capture_macos.sh stop
  bash modules/01_global_camera/run_capture_macos.sh capture --run-dir runs/<run_id> --duration-s 10

Notes:
  - The recommended first command on this Mac is:
      bash modules/01_global_camera/run_capture_macos.sh install-passwordless-sudo
  - Until passwordless sudo is installed, the wrapper will still work,
    but it may prompt for your macOS password.
EOF
}

if [[ "${1:-}" == "--help-wrapper" || "${1:-}" == "help" || $# -eq 0 ]]; then
  usage
  exit 0
fi

active_control_script() {
  if [[ -x "${INSTALLED_CONTROL_SCRIPT}" ]]; then
    printf '%s\n' "${INSTALLED_CONTROL_SCRIPT}"
    return 0
  fi
  printf '%s\n' "${CONTROL_SCRIPT}"
}

can_run_passwordless() {
  [[ -x "${INSTALLED_CONTROL_SCRIPT}" ]] || return 1
  sudo -n "${INSTALLED_CONTROL_SCRIPT}" help >/dev/null 2>&1
}

run_with_sudo() {
  local script
  script="$(active_control_script)"
  if can_run_passwordless; then
    sudo -n "${INSTALLED_CONTROL_SCRIPT}" "$@"
  elif [[ "${script}" == "${CONTROL_SCRIPT}" ]]; then
    sudo bash "${CONTROL_SCRIPT}" "$@"
  else
    sudo "${script}" "$@"
  fi
}

run_capture() {
  run_with_sudo run-once "$@"
}

run_control() {
  run_with_sudo "$@"
}

COMMAND="${1:-}"
shift || true

case "${COMMAND}" in
  install-passwordless-sudo)
    bash "${INSTALL_SUDOERS_SCRIPT}"
    ;;
  warmup-sudo)
    if can_run_passwordless; then
      echo "Passwordless sudo is already enabled for RealSense control."
    else
      sudo -v
      echo "sudo credentials refreshed for this session."
    fi
    ;;
  list)
    run_control list "$@"
    ;;
  stream)
    DURATION="${1:-10}"
    PREVIEW_DIR="${2:-/tmp/realsense_stream_preview}"
    run_control stream --duration-s "${DURATION}" --preview-dir "${PREVIEW_DIR}"
    ;;
  test)
    DURATION="${1:-5}"
    OUTPUT_DIR="${2:-/tmp/realsense_test}"
    run_capture --output-dir "${OUTPUT_DIR}" --duration-s "${DURATION}"
    ;;
  start)
    run_control start "$@"
    ;;
  stop)
    run_control stop
    ;;
  status)
    run_control status
    ;;
  capture)
    run_capture "$@"
    ;;
  *)
    echo "Unknown command: ${COMMAND}"
    echo
    usage
    exit 1
    ;;
esac

exit 0
