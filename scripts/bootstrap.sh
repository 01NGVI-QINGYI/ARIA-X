#!/usr/bin/env bash
# Prepare a fresh ARIA-X checkout for local development. Offline and stdlib-only:
# this script never installs packages and never needs an API key.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${ROOT}/scripts/_common.sh"

PY="$(aria_x_python)"
cd "${ROOT}"

echo "ARIA-X bootstrap"
echo "  root:   ${ROOT}"
echo "  python: ${PY} ($("${PY}" -c 'import platform; print(platform.python_version())'))"

"${PY}" - <<'PYEOF'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("ARIA-X requires Python >= 3.11")
print("  version check: ok")
PYEOF

mkdir -p data runs
echo "  created: data/ runs/"

PYTHONPATH="${ROOT}/src" "${PY}" -c "import aria_x; print(f'  package: aria_x {aria_x.__version__} ({aria_x.__license__})')"
PYTHONPATH="${ROOT}/src" "${PY}" -m aria_x init --dir "${ROOT}" >/dev/null
echo "  config:  configs/aria-x.toml (edit scope.targets and [llm] before running)"

cat <<'NEXT'

Next steps:
  1. Read docs/SECURITY.md — ARIA-X only runs against authorized targets.
  2. Edit configs/aria-x.toml: set scope.authorized = true and scope.targets.
  3. Run the offline demo:        bash scripts/verify.sh
     or:                          PYTHONPATH=src python -m aria_x demo
  4. Start the web console:       bash scripts/dev.sh serve
NEXT
