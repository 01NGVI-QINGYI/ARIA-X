#!/usr/bin/env bash
# Shared helpers for ARIA-X developer scripts. Source this file; do not execute it.
# No pip, no virtualenv, no network: ARIA-X only needs CPython 3.11+ and its stdlib.

set -euo pipefail

aria_x_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${here}/.." && pwd
}

# Locate a suitable interpreter, in priority order:
#   1. $ARIA_X_PYTHON (explicit override)
#   2. python3.12 on PATH
#   3. python3.11 on PATH
#   4. python3 on PATH, if it is >= 3.11
aria_x_python() {
  local candidate
  if [[ -n "${ARIA_X_PYTHON:-}" ]]; then
    if [[ -x "${ARIA_X_PYTHON}" ]]; then
      printf '%s\n' "${ARIA_X_PYTHON}"
      return 0
    fi
    echo "ARIA_X_PYTHON is set but not executable: ${ARIA_X_PYTHON}" >&2
    return 1
  fi
  if command -v python3.12 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3.12)"
    return 0
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3.11)"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    candidate="$(command -v python3)"
    if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  fi
  echo "No Python >= 3.11 found. Set ARIA_X_PYTHON=/path/to/python3.12" >&2
  return 1
}

