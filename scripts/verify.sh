#!/usr/bin/env bash
# ARIA-X verification gate: unit tests + the offline end-to-end demo.
#
# Requirements: a CPython >= 3.11 interpreter. No pip installs, no network, no
# API key. Exit code 0 means both the test suite and the demo succeeded.
#
#   bash scripts/verify.sh              # full gate
#   ARIA_X_SKIP_DEMO=1 bash scripts/verify.sh   # tests only (useful on slow disks)
#   ARIA_X_PYTHON=/path/to/python3.12 bash scripts/verify.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${ROOT}/scripts/_common.sh"
PY="$(aria_x_python)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONUNBUFFERED=1
cd "${ROOT}"

echo "=============================================================="
echo " ARIA-X verify"
echo "   root:   ${ROOT}"
echo "   python: ${PY}"
echo "=============================================================="
"${PY}" - <<'PYEOF'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"ARIA-X requires Python >= 3.11, got {sys.version.split()[0]}")
print(f"python {sys.version.split()[0]}: ok")
PYEOF

echo
echo "[1/3] importing the package"
"${PY}" - <<'PYEOF'
import aria_x
import aria_x.agents
import aria_x.config
import aria_x.engine
import aria_x.events
import aria_x.llm
import aria_x.models
import aria_x.store
import aria_x.tools
print(f"aria_x {aria_x.__version__} import: ok")
PYEOF

echo
echo "[2/3] unit tests"
"${PY}" -m unittest discover -s tests -v

if [[ "${ARIA_X_SKIP_DEMO:-0}" == "1" ]]; then
  echo
  echo "[3/3] offline demo skipped (ARIA_X_SKIP_DEMO=1)"
  echo
  echo "ARIA-X verify: PASS (tests only)"
  exit 0
fi

echo
echo "[3/3] offline end-to-end demo"
DEMO_OUT="$(mktemp -t aria-x-demo-verify.XXXXXX)"
trap 'rm -f "${DEMO_OUT}"' EXIT
if ! "${PY}" -m aria_x demo --quiet >"${DEMO_OUT}" 2>&1; then
  echo "demo failed; last output:" >&2
  tail -80 "${DEMO_OUT}" >&2
  exit 1
fi
grep -E '\[aria-x\] (status|report|summary)' "${DEMO_OUT}" || tail -40 "${DEMO_OUT}"
if ! grep -q 'findings=' "${DEMO_OUT}"; then
  echo "demo did not report findings; output:" >&2
  tail -80 "${DEMO_OUT}" >&2
  exit 1
fi
FINDINGS="$(grep -o 'findings=[^ ]*' "${DEMO_OUT}" | head -1 | cut -d= -f2 || true)"
if [[ "${FINDINGS}" == "0" ]]; then
  echo "demo completed with zero findings" >&2
  exit 1
fi

echo
echo "ARIA-X verify: PASS"
