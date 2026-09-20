#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "run_d435_macos.sh is for macOS only."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_SCRIPT="${ROOT_DIR}/control_capture_macos.sh"
INSTALL_SUDOERS_SCRIPT="${ROOT_DIR}/install_sudoers_macos.sh"
INSTALLED_CONTROL_ROOT="/usr/local/libexec/umift-laptop-alignment-d435"
INSTALLED_CONTROL_SCRIPT="${INSTALLED_CONTROL_ROOT}/control_capture_macos.sh"

usage() {
  cat <<EOF
Usage:
  bash modules/04_laptop_alignment_export/umift_laptop_alignment/capture/receivers/d435/run_d435_macos.sh <command> [args...]

Commands:
  install-passwordless-sudo
      One-time setup. Installs a root-owned 04/D435 control bundle and a narrow sudoers whitelist.

  warmup-sudo
      Check whether passwordless sudo is enabled. If not, refresh sudo credentials once.

  list
      List RealSense devices.

  stream [capture_d435i.py args...]
      Run a foreground live controlled stream.

  test [duration_s] [output_dir]
      Run a short capture test.

  start [capture_d435i.py args...]
      Start a background capture session.

  stop
      Stop the background capture session.

  status
      Show background capture session state.

  run-once [capture_d435i.py args...]
      Run one foreground capture.

  help
      Show this message.

Recommended first command:
  bash modules/04_laptop_alignment_export/umift_laptop_alignment/capture/receivers/d435/run_d435_macos.sh install-passwordless-sudo
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

run_control() {
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

COMMAND="${1:-}"
shift || true

case "${COMMAND}" in
  install-passwordless-sudo)
    bash "${INSTALL_SUDOERS_SCRIPT}"
    ;;
  warmup-sudo)
    if can_run_passwordless; then
      echo "Passwordless sudo is already enabled for 04/D435 control."
    else
      sudo -v
      echo "sudo credentials refreshed for this session."
    fi
    ;;
  list)
    run_control list "$@"
    ;;
  stream)
    run_control stream "$@"
    ;;
  test)
    DURATION="${1:-5}"
    OUTPUT_DIR="${2:-/tmp/umift_d435_test}"
    run_control run-once --output-dir "${OUTPUT_DIR}" --duration-s "${DURATION}"
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
  run-once)
    run_control run-once "$@"
    ;;
  *)
    echo "Unknown command: ${COMMAND}"
    echo
    usage
    exit 1
    ;;
esac

exit 0
