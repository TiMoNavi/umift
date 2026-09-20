#!/usr/bin/env bash
set -euo pipefail

ENTRYPOINT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE04_ROOT="$(cd "${ENTRYPOINT_DIR}/.." && pwd)"
D435_INSTALLER="${MODULE04_ROOT}/umift_laptop_alignment/capture/receivers/d435/install_sudoers_macos.sh"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || "${1:-}" == "help" ]]; then
  cat <<EOF
Usage:
  bash modules/04_laptop_alignment_export/entrypoints/install_sudoers_macos.sh

One-time macOS setup for this computer:
  - installs a root-owned 04/D435 control bundle
  - writes a narrow sudoers whitelist for that fixed entrypoint
  - lets receiver_web_gui.py start D435 later with sudo -n
EOF
  exit 0
fi

if [[ ! -f "${D435_INSTALLER}" ]]; then
  echo "Missing D435 sudoers installer: ${D435_INSTALLER}"
  exit 1
fi

exec bash "${D435_INSTALLER}" "$@"
