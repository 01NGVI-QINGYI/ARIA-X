#!/usr/bin/env bash
# Developer entry point: fast checks plus the local web console.
#
#   bash scripts/dev.sh            # unit tests + offline demo (same as verify.sh)
#   bash scripts/dev.sh test       # unit tests only
#   bash scripts/dev.sh demo       # offline end-to-end demo only
#   bash scripts/dev.sh serve      # web console on 127.0.0.1:8787
#   bash scripts/dev.sh doctor     # environment/configuration checks
#   bash scripts/dev.sh tools      # list the tool catalog
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${ROOT}/scripts/_common.sh"
PY="$(aria_x_python)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
cd "${ROOT}"

cmd="${1:-all}"
shift || true

case "${cmd}" in
  test)
    exec "${PY}" -m unittest discover -s tests -v "$@"
    ;;
  demo)
    exec "${PY}" -m aria_x demo "$@"
    ;;
  serve)
    exec "${PY}" -m aria_x serve --open "$@"
    ;;
  doctor)
    exec "${PY}" -m aria_x doctor "$@"
    ;;
  tools)
    exec "${PY}" -m aria_x tools "$@"
    ;;
  all)
    bash "${ROOT}/scripts/verify.sh"
    ;;
  *)
    echo "usage: bash scripts/dev.sh [all|test|demo|serve|doctor|tools]" >&2
    exit 2
    ;;
esac
