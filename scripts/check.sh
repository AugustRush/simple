#!/usr/bin/env bash
# The one command: everything CI runs, runnable on the machine you are sitting at.
#
#   bash scripts/check.sh              # the whole suite
#   bash scripts/check.sh tests/ -k web # ...or a slice of it, same gate
#
# Why one script rather than a list in a workflow file: a CI that runs a sequence nobody
# can reproduce locally is a CI that reports on a different program.  The workflow calls
# this, so "green locally" and "green on the runner" are the same claim.
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. The lockfile and pyproject agree.  An extra declared but not locked makes
#    `uv sync --extra ...` fail for the next person, and it is checked here because
#    nothing else reads pyproject.
if command -v uv >/dev/null 2>&1; then
  uv lock --check
else
  echo "check: uv not on PATH -- skipping the lockfile check" >&2
fi

# 2. The suite.  Two things in it are conditional and say so rather than passing quietly:
#    - tests/test_web_ui_e2e.py skips without playwright (the optional [e2e] extra);
#    - tests/test_web_bundle_freshness.py fails when frontend/ changed without a rebuild.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
  elif command -v uv >/dev/null 2>&1; then
    PYTHON="uv run python"
  else
    PYTHON=python3
  fi
fi

# shellcheck disable=SC2086  # PYTHON may be "uv run python": two words on purpose
exec $PYTHON -m pytest -q -p no:cacheprovider "$@"
