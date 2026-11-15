#!/usr/bin/env bash
set -euo pipefail
: "${ZVONILKA_TEST_REDIS_URL:?Set URL of dedicated test Redis instance}"
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
env -u VIRTUAL_ENV uv run python -m zvonilka.testing.runner \
  --start tests --pattern 'test_redis*.py' --require-no-skips "$@"
