#!/usr/bin/env bash
# Run before every release: known vulnerabilities in the Python and web dependencies, and KROVA's own security tests
# (one business never sees another's records, logs never hold phone numbers, key rotation works).
#
#   bash scripts/security_check.sh
#
# Known and accepted today (2026-11-02), so not a reason to stop a release:
#   - starlette 0.50: remaining issues need FastAPI 0.129+, which changes how routes are listed (activity log).
#     Two do not apply (no StaticFiles, no HTTPEndpoint); Host/path ones are limited by Caddy's site match and
#     the form one by Caddy's 20 MB request limit.
#   - pytest: test tool only, never runs on the server.
#   - postcss inside Next.js 15: build-time only, needs Next 16.
set -uo pipefail
cd "$(dirname "$0")/.."
status=0

echo "== Python dependencies"
python -m pip_audit --version >/dev/null 2>&1 || python -m pip install -q pip-audit
python -m pip_audit -r requirements.txt --progress-spinner off || status=1

echo "== Web dependencies"
if [ -d ../krova-web ]; then
  (cd ../krova-web && npm audit --omit=dev) || status=1
fi

echo "== Security tests"
python -m pytest -q -p no:warnings tests/security || status=1

exit $status
